"""The repair loop.

Candidate acceptance (see CLAUDE.md + DESIGN.md): a candidate is kept only if it restores at
least one still-broken case, breaks zero previously-passing cases, and no case restored by an
earlier accepted candidate relapses — all measured on a FULL-suite verification run of N reps
against the candidate model. Accepted candidates stack (endpoint fix first, then behavioral
fixes). The final SAFE WITH PATCH verdict additionally requires that ALL originally-regressed
cases are restored; partial restoration ends in STAY PINNED with the evidence.

Screening: before paying for a full verify run, a candidate is screened on just the
still-broken cases; it must restore at least one of them to earn verification.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from upshift import recorder
from upshift.differ import SIG_THINKING_BLOCK_INVALID, DiffResult, failure_signatures
from upshift.providers import Provider
from upshift.repair.playbook import generate_candidates
from upshift.schemas import (
    LABEL_REGRESSED,
    OUTCOME_PASS,
    Patch,
    outcome,
)

# Priority order for signature-driven candidate generation (hard API breaks first).
_SIGNATURE_PRIORITY = [
    "api_error_forced_tool_choice",
    "api_error_unsupported_sampling_params",
    "api_error_tools_reasoning",
    "api_error_other",
    "duplicate_tool_calls",
    "acting_past_goal",
    "skipped_tool_hallucination",
    "serialized_tool_calls",
    "reduced_retrieval_calls",
    "wrong_or_missing_tool_call",
    "other_behavioral",
]
# thinking_block_invalid is deliberately ABSENT from that list: no repair of an allowed type
# fixes it, so the loop refuses instead of burning budget on candidates that cannot work.
THINKING_REFUSAL = (
    "no repair candidate exists within the allowed repair types for thinking_block_invalid "
    "(400 'Invalid `signature` in `thinking` block'). The fix is runtime history handling, "
    "not an agent-file edit: strip the invalidated run of its thinking blocks before "
    "replaying it, or set thinking.block_binding.prefix_mismatch_behavior: \"drop_block\" "
    "under the thinking-binding-controls-2026-08-01 beta. See DESIGN.md, "
    "\"Documented 5 -> 5.1 changes as detectors + repairs\" item 2."
)


def thinking_refusal_lines(
    per_case_sigs: dict[str, list[str]], unrestored: set[str], already_logged: set[str]
) -> list[str]:
    """REFUSAL log lines for still-unrestored cases carrying ``thinking_block_invalid``.

    Mutates ``already_logged`` so a case is refused once per repair run, not once per
    iteration. Pure otherwise: it decides nothing about acceptance.
    """
    lines = []
    for case_id in sorted(unrestored):
        if case_id in already_logged:
            continue
        if SIG_THINKING_BLOCK_INVALID in (per_case_sigs.get(case_id) or []):
            already_logged.add(case_id)
            lines.append(f"REFUSAL {case_id}: {THINKING_REFUSAL}")
    return lines


@dataclass
class RepairOutcome:
    accepted_patches: list[Patch]
    restored: list[str]
    unrestored: list[str]
    tried: int
    budget: int
    log: list[str] = field(default_factory=list)
    final_verify_run_id: str | None = None


def _copy_agent_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _apply(patch: Patch, agent_dir: Path) -> None:
    for edit in patch.edits:
        (agent_dir / edit.file).write_text(edit.new_content)


def _case_pass_counts(run_directory: Path, case_ids: list[str]) -> dict[str, tuple[int, int]]:
    counts = {}
    for case_id in case_ids:
        reps = recorder.load_case_reps(run_directory, case_id)
        if not reps:
            raise ValueError(f"no reps recorded for case {case_id} in {run_directory}")
        counts[case_id] = (sum(1 for r in reps if r.passed), len(reps))
    return counts


def _config_hash(agent_dir: Path) -> str:
    """Short stable hash of the three patchable files, used in repair run ids so identical
    re-runs resume from disk while a changed candidate lineage gets fresh run dirs."""
    import hashlib
    import json

    raw = json.loads((agent_dir / "agent.json").read_text())
    h = hashlib.sha256()
    for rel in ("agent.json", raw["system_prompt_file"], raw["tools_file"]):
        h.update((agent_dir / rel).read_bytes())
    return h.hexdigest()[:8]


def _baseline_reps(runs_root: str | Path, baseline_run_id: str, case_id: str):
    """The baseline run's reps for one case, or None when that run dir is not on disk."""
    directory = recorder.run_dir(runs_root, baseline_run_id)
    if not (directory / "cases" / case_id).is_dir():
        return None
    return recorder.load_case_reps(directory, case_id) or None


def _ordered_signatures(per_case: dict[str, list[str]]) -> list[str]:
    present = {sig for sigs in per_case.values() for sig in sigs}
    return [s for s in _SIGNATURE_PRIORITY if s in present]


def repair(
    *,
    original_agent_dir: str | Path,
    work_dir: str | Path,
    provider: Provider,
    candidate_model: str,
    baseline_diff: DiffResult,
    n_reps: int,
    runs_root: str | Path,
    run_prefix: str,
    budget: int = 6,
    workers: int = 4,
) -> RepairOutcome:
    original_agent_dir = Path(original_agent_dir)
    work_dir = Path(work_dir)
    _copy_agent_dir(original_agent_dir, work_dir)

    # Late import: runner imports checks.py/agent_loop.py which other components own.
    from upshift.runner import run_suite

    thresholds = baseline_diff.baseline_manifest["thresholds"]
    all_case_ids = [c.case_id for c in baseline_diff.cases]
    regressed = sorted(c.case_id for c in baseline_diff.cases if c.label == LABEL_REGRESSED)
    protected = sorted(
        c.case_id for c in baseline_diff.cases if c.candidate_outcome == OUTCOME_PASS
    )
    per_case_sigs = {
        c.case_id: c.failure_signatures for c in baseline_diff.cases if c.case_id in regressed
    }

    unrestored = set(regressed)
    restored: set[str] = set()
    accepted: list[Patch] = []
    tried = 0
    log: list[str] = [
        (
            f"repair start: {len(regressed)} regressed case(s), {len(protected)} protected "
            f"passing case(s), budget {budget} candidates"
        )
    ]
    tried_ids: set[str] = set()
    final_verify_run_id: str | None = None
    refused: set[str] = set()
    log.extend(thinking_refusal_lines(per_case_sigs, unrestored, refused))

    while unrestored and tried < budget:
        candidates = [
            p
            for p in generate_candidates(work_dir, _ordered_signatures(per_case_sigs))
            if p.id not in tried_ids
        ]
        if not candidates:
            log.append("no further candidates for the observed failure signatures; giving up")
            break

        progressed = False
        for patch in candidates:
            if tried >= budget:
                break
            tried += 1
            tried_ids.add(patch.id)
            log.append(f"candidate {tried}/{budget}: [{patch.repair_type}] {patch.id} — "
                       f"{patch.description}")

            with tempfile.TemporaryDirectory(prefix="upshift-repair-") as tmp:
                trial_dir = Path(tmp) / "agent"
                _copy_agent_dir(work_dir, trial_dir)
                _apply(patch, trial_dir)

                # Run ids carry a hash of the trial config: identical re-runs resume from
                # disk for free, while a changed candidate lineage gets fresh run dirs.
                cfg = _config_hash(trial_dir)
                screen_id = f"{run_prefix}-c{tried:02d}-{cfg}-screen"
                run_suite(
                    trial_dir,
                    provider,
                    screen_id,
                    n_reps=n_reps,
                    model_override=candidate_model,
                    runs_root=runs_root,
                    case_ids=sorted(unrestored),
                    workers=workers,
                    notes=f"repair screen for candidate {patch.id}",
                )
                screen_counts = _case_pass_counts(
                    recorder.run_dir(runs_root, screen_id), sorted(unrestored)
                )
                screen_restored = {
                    c
                    for c, (k, n) in screen_counts.items()
                    if outcome(k, n, thresholds["pass"], thresholds["fail"]) == OUTCOME_PASS
                }
                if not screen_restored:
                    log.append(
                        f"  screen: 0/{len(unrestored)} broken cases restored — rejected "
                        f"without full verification"
                    )
                    continue
                log.append(
                    f"  screen: {len(screen_restored)}/{len(unrestored)} broken cases "
                    f"restored — running full verification"
                )

                verify_id = f"{run_prefix}-c{tried:02d}-{cfg}-verify"
                run_suite(
                    trial_dir,
                    provider,
                    verify_id,
                    n_reps=n_reps,
                    model_override=candidate_model,
                    runs_root=runs_root,
                    workers=workers,
                    notes=f"repair full verification for candidate {patch.id}",
                )
                verify_counts = _case_pass_counts(
                    recorder.run_dir(runs_root, verify_id), all_case_ids
                )

                def is_pass(k: int, n: int) -> bool:
                    return outcome(k, n, thresholds["pass"], thresholds["fail"]) == OUTCOME_PASS

                # Restoration claims must survive screen AND verify combined (2N reps on
                # the same config) — a lucky single-run pass does not count as restored.
                newly_restored = set()
                for case_id in unrestored:
                    sk, sn = screen_counts[case_id]
                    vk, vn = verify_counts[case_id]
                    if is_pass(sk + vk, sn + vn):
                        newly_restored.add(case_id)

                # A protected or earlier-restored case that dips below PASS in one N-rep
                # sample is a SUSPECT, not a verdict: adjudicate on N more reps of the
                # same trial config and decide on the combined 2N at the same threshold.
                # Same evidence bar as restoration claims; thresholds never change.
                suspects = sorted(
                    c
                    for c in (set(protected) | restored)
                    if not is_pass(*verify_counts[c])
                )
                confirmed_bad: list[str] = []
                if suspects and newly_restored:
                    adj_id = f"{run_prefix}-c{tried:02d}-{cfg}-adj"
                    run_suite(
                        trial_dir,
                        provider,
                        adj_id,
                        n_reps=n_reps,
                        model_override=candidate_model,
                        runs_root=runs_root,
                        case_ids=suspects,
                        workers=workers,
                        notes=f"adjudication of contested cases for candidate {patch.id}",
                    )
                    adj_counts = _case_pass_counts(recorder.run_dir(runs_root, adj_id), suspects)
                    for case_id in suspects:
                        vk, vn = verify_counts[case_id]
                        ak, an = adj_counts[case_id]
                        verdict = "cleared" if is_pass(vk + ak, vn + an) else "CONFIRMED"
                        log.append(
                            f"  adjudication {case_id}: verify {vk}/{vn} + extra {ak}/{an} "
                            f"= {vk + ak}/{vn + an} — {verdict}"
                        )
                        if verdict == "CONFIRMED":
                            confirmed_bad.append(case_id)
                elif suspects:
                    confirmed_bad = suspects  # nothing restored anyway; no need to spend

                broken = sorted(c for c in confirmed_bad if c in protected)
                relapsed = sorted(c for c in confirmed_bad if c in restored)

                if newly_restored and not broken and not relapsed:
                    _copy_agent_dir(trial_dir, work_dir)
                    accepted.append(patch)
                    restored |= newly_restored
                    unrestored -= newly_restored
                    final_verify_run_id = verify_id
                    log.append(
                        f"  ACCEPTED: restored {sorted(newly_restored)}; "
                        f"0 previously-passing cases broken; "
                        f"{len(unrestored)} regressed case(s) remain"
                    )
                    # Refresh failure signatures for what remains from this verify run.
                    per_case_sigs = {}
                    for case_id in unrestored:
                        reps = recorder.load_case_reps(
                            recorder.run_dir(runs_root, verify_id), case_id
                        )
                        # Behavioral signatures compare against the BASELINE run, so the
                        # refreshed classification needs the same case's baseline reps.
                        per_case_sigs[case_id] = failure_signatures(
                            [r for r in reps if not r.passed],
                            _baseline_reps(runs_root, baseline_diff.baseline_run_id, case_id),
                        )
                    log.extend(thinking_refusal_lines(per_case_sigs, unrestored, refused))
                    progressed = True
                    break
                reasons = []
                if not newly_restored:
                    reasons.append("verification restored no broken case")
                if broken:
                    reasons.append(f"broke previously-passing case(s) {broken}")
                if relapsed:
                    reasons.append(f"relapsed earlier-restored case(s) {relapsed}")
                log.append(f"  REJECTED: {'; '.join(reasons)}")
        if not progressed:
            if tried >= budget:
                log.append(f"repair budget exhausted ({budget} candidates tried)")
            else:
                log.append("all current candidates rejected; giving up")
            break

    if unrestored:
        log.append(
            f"repair end: {len(restored)}/{len(regressed)} regressed cases restored; "
            f"unrestored: {sorted(unrestored)}"
        )
    else:
        log.append(f"repair end: all {len(regressed)} regressed cases restored")

    return RepairOutcome(
        accepted_patches=accepted,
        restored=sorted(restored),
        unrestored=sorted(unrestored),
        tried=tried,
        budget=budget,
        log=log,
        final_verify_run_id=final_verify_run_id,
    )
