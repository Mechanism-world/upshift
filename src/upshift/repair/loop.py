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
from upshift.differ import DiffResult, failure_signatures
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
    "api_error_tools_reasoning",
    "api_error_other",
    "duplicate_tool_calls",
    "acting_past_goal",
    "skipped_tool_hallucination",
    "wrong_or_missing_tool_call",
    "other_behavioral",
]


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


def _case_outcomes(run_directory: Path, case_ids: list[str], thresholds: dict) -> dict[str, str]:
    outcomes = {}
    for case_id in case_ids:
        reps = recorder.load_case_reps(run_directory, case_id)
        if not reps:
            raise ValueError(f"no reps recorded for case {case_id} in {run_directory}")
        passes = sum(1 for r in reps if r.passed)
        outcomes[case_id] = outcome(passes, len(reps), thresholds["pass"], thresholds["fail"])
    return outcomes


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
        f"repair start: {len(regressed)} regressed case(s), {len(protected)} protected "
        f"passing case(s), budget {budget} candidates"
    ]
    tried_ids: set[str] = set()
    final_verify_run_id: str | None = None

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

                screen_id = f"{run_prefix}-c{tried:02d}-screen"
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
                screen_outcomes = _case_outcomes(
                    recorder.run_dir(runs_root, screen_id), sorted(unrestored), thresholds
                )
                screen_restored = {c for c, o in screen_outcomes.items() if o == OUTCOME_PASS}
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

                verify_id = f"{run_prefix}-c{tried:02d}-verify"
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
                verify_outcomes = _case_outcomes(
                    recorder.run_dir(runs_root, verify_id), all_case_ids, thresholds
                )
                newly_restored = {c for c in unrestored if verify_outcomes[c] == OUTCOME_PASS}
                broken = sorted(c for c in protected if verify_outcomes[c] != OUTCOME_PASS)
                relapsed = sorted(c for c in restored if verify_outcomes[c] != OUTCOME_PASS)

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
                        per_case_sigs[case_id] = failure_signatures(
                            [r for r in reps if not r.passed]
                        )
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
