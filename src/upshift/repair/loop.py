"""The repair loop.

Candidate acceptance (see CLAUDE.md + DESIGN.md): a candidate is kept only if it restores at
least one still-broken case, breaks zero previously-passing cases, and no case restored by an
earlier accepted candidate relapses — all measured on a FULL-suite verification run of N reps
against the candidate model. Accepted candidates stack (endpoint fix first, then behavioral
fixes). The final SAFE WITH PATCH verdict additionally requires that ALL originally-regressed
cases are restored; partial restoration ends in STAY PINNED with the evidence.

Screening: EVERY candidate a signature round produces is screened on just the still-broken
cases before any of them is verified. The restorers are then ranked — most cases restored
(full restoration first), then no disclosed change of capability or cost, then the playbook's
own rank — and the best one is verified; on rejection the loop falls to the next. Accepting
the first candidate that happened to work published a wrong STAY PINNED once (rescue-ops
`ghisdk-127`, waku: a sibling in the same generation restored all nine cases where the
accepted one restored eight) and hid a maintainer's own fix behind a disclosed one
(`ghc-062`, crispen). Screening costs one screen run per sibling; `budget` still bounds the
total number of candidates tried.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from upshift import differ, recorder
from upshift.budget import CostCeiling, CostCeilingExceeded
from upshift.differ import SIG_THINKING_BLOCK_INVALID, DiffResult, failure_signatures
from upshift.providers import Provider
from upshift.repair.playbook import disclosures_for, generate_candidates, rank_for
from upshift.schemas import (
    LABEL_REGRESSED,
    OUTCOME_PASS,
    Patch,
    outcome,
)

#: Priority order for signature-driven candidate generation, DERIVED from the differ's own
#: taxonomy (hard API breaks first) minus the signatures the differ documents as having no
#: repair. It used to be a hand-maintained copy, which meant a signature added to differ.py
#: was silently invisible to the repair loop — a new break would be detected, reported, and
#: then not even attempted. There is one list, and `SIGNATURES_WITHOUT_REPAIRS` is the only
#: legitimate way to be absent from it (differ.py holds the reason for each).
_SIGNATURE_PRIORITY = [
    sig for sig in differ.SIGNATURE_PRIORITY if sig not in differ.SIGNATURES_WITHOUT_REPAIRS
]
# thinking_block_invalid and harness_error are absent by that rule: no repair of an allowed
# type fixes either, so the loop refuses instead of burning budget on candidates that cannot
# work.
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


#: Salt mixed into the per-(case, rep) seed of the fresh final verification (DESIGN.md §D).
#: The final run must be a NEW sample, not a replay of the sample that selected the
#: candidate: reusing the selection seeds would let a candidate chosen because it happened
#: to work on those draws be confirmed by the same draws.
FINAL_RUN_SEED_SALT = "upshift-final-verification"

#: `run_suite` reads the module-level `runner.seed_for`; when it does not (yet) accept a
#: `seed_salt` argument, the final run swaps that function for the duration. Serialised
#: because the swap is process-global and `run_suite` itself is threaded.
_SEED_SALT_LOCK = threading.Lock()


@contextlib.contextmanager
def _salted_seeds(run_suite, salt: str):
    """Yield the kwargs `run_suite` needs for salted seeds, patching `runner.seed_for` if it
    has no `seed_salt` parameter of its own. Restores the original either way."""
    try:
        supported = "seed_salt" in inspect.signature(run_suite).parameters
    except (TypeError, ValueError):  # pragma: no cover - a C callable would land here
        supported = False
    if supported:
        yield {"seed_salt": salt}
        return

    from upshift import runner as runner_module

    with _SEED_SALT_LOCK:
        original = runner_module.seed_for

        def salted(case_id: str, rep: int) -> int:
            return int(hashlib.sha256(f"{salt}:{case_id}:{rep}".encode()).hexdigest()[:8], 16)

        runner_module.seed_for = salted
        try:
            yield {}
        finally:
            runner_module.seed_for = original


@dataclass
class RepairOutcome:
    accepted_patches: list[Patch]
    restored: list[str]
    unrestored: list[str]
    tried: int
    budget: int
    log: list[str] = field(default_factory=list)
    final_verify_run_id: str | None = None
    #: The FRESH full-suite run of the stacked patch, at new seeds, that the verdict rests on
    #: (DESIGN.md §D). None when no candidate was accepted or `final_verify=False`.
    final_run_id: str | None = None
    #: Every screen / verify / adjudication run. These SELECTED the candidates; they are not
    #: the evidence for them, and the report lists them under that name.
    selection_runs: list[str] = field(default_factory=list)
    #: Cases that passed on the candidate before repair and fail on the final run. Empty is
    #: a measured result here, not an assumption.
    broken_by_patch: list[str] = field(default_factory=list)
    #: Cases the candidate was accepted for that the fresh final run did not confirm.
    unconfirmed_by_final: list[str] = field(default_factory=list)
    protected_cases: list[str] = field(default_factory=list)
    #: (verify runs performed) x (protected cases): how many times collateral damage was
    #: actually looked for. Zero with protected_cases == 0 is the "nothing to protect" case.
    collateral_checks: int = 0
    evidence_label: str | None = None
    #: Suspect cases a candidate's adjudication run could NOT measure, because the cost
    #: ceiling stopped it. The candidate is rejected rather than accepted on the unadjudicated
    #: sample, and the verdict is INCONCLUSIVE (DESIGN.md §D) rather than SAFE WITH PATCH.
    adjudication_skipped: list[str] = field(default_factory=list)
    #: True when the loop stopped because its cost ceiling was reached mid-decision. The
    #: verdict reads it and refuses to conclude; see `verdict.decide`.
    cost_stopped: bool = False


@dataclass
class _Screened:
    """One candidate's screen result, kept so the round can rank siblings against each other.

    The trial directory is NOT kept: it is rebuilt from `work_dir` + the patch when (and only
    when) this candidate is verified, which is a deterministic copy-and-apply and keeps the
    peak disk cost at one trial dir rather than one per sibling.
    """

    patch: Patch
    index: int  # the candidate's number in this repair run; part of its run ids
    cfg: str  # config hash of the trial dir, so an identical re-run resumes from disk
    screen_id: str
    screen_counts: dict[str, tuple[int, int]]
    restored: set[str]
    order: int  # position in the playbook's own ordering; the last tie-break


def rank_screened(screened: list[_Screened], still_broken: set[str]) -> list[_Screened]:
    """The restorers, best first. Non-restorers are dropped: they never earn a verify run.

    The order is a policy, and it is the policy the two rescue-ops failures argue for:

    1. **Full restoration first, then most cases restored.** A candidate that fixes the whole
       suite is not merely a better version of one that fixes most of it — it is the
       difference between SAFE WITH PATCH and STAY PINNED (`ghisdk-127`).
    2. **No disclosure before a disclosure.** A repair that changes what the agent GUARANTEES
       (a removed forced tool_choice) or what it COSTS (a raised reasoning effort) is a
       different agent with a green suite. When a repair that changes neither restores the
       same cases, it is strictly better, and it is the one a maintainer would have written
       (`ghc-062`).
    3. **The playbook's rank**, so a transport/spelling fix still precedes a behavioural one.
    4. The playbook's own order, so nothing about this ranking is arbitrary where the three
       criteria above are silent.

    Ranking decides only which candidate is VERIFIED first. Acceptance thresholds,
    adjudication and the fresh final verification are untouched.
    """
    return sorted(
        (s for s in screened if s.restored),
        key=lambda s: (
            0 if still_broken and s.restored >= still_broken else 1,
            -len(s.restored),
            1 if disclosures_for(s.patch.id) else 0,
            rank_for(s.patch.id),
            s.order,
        ),
    )


def _copy_agent_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _apply(patch: Patch, agent_dir: Path) -> None:
    for edit in patch.edits:
        (agent_dir / edit.file).write_text(edit.new_content)


@contextlib.contextmanager
def _trial(work_dir: Path, patch: Patch):
    """A throwaway copy of the current stacked agent with `patch` applied.

    Built twice for a candidate that is both screened and verified. That is deliberate: it is
    a local file copy, it is byte-identical both times (so the config hash, and therefore the
    run ids, agree), and it means screening N siblings costs one trial directory rather than N.
    """
    with tempfile.TemporaryDirectory(prefix="upshift-repair-") as tmp:
        trial_dir = Path(tmp) / "agent"
        _copy_agent_dir(work_dir, trial_dir)
        _apply(patch, trial_dir)
        yield trial_dir


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
    cost_ceiling: CostCeiling | None = None,
    final_verify: bool = True,
    runner_options: Any = None,
    capture_session: Any = None,
) -> RepairOutcome:
    """``final_verify`` (DESIGN.md §D): after the last accepted candidate, re-run the stacked
    patch on the FULL suite at fresh seeds as ``<run_prefix>-final`` and let the verdict rest
    on that, not on the runs that selected the candidates. Setting it False skips the run and
    labels the outcome ``selection_evidence_only`` — cheaper, and honestly weaker."""
    original_agent_dir = Path(original_agent_dir)
    work_dir = Path(work_dir)
    _copy_agent_dir(original_agent_dir, work_dir)

    # Forwarded verbatim to every run this loop makes, so a repair run executes under exactly
    # the authorization and wire-capture the CLI granted the baseline and candidate runs — a
    # screen run that quietly ran unauthorized, or without the capture, would not be evidence
    # about the same thing. Empty for an ordinary adapter agent.
    execution = {"runner_options": runner_options, "capture_session": capture_session}

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
    #: Candidate ids already screened against the CURRENT stacked agent. Cleared whenever a
    #: candidate is accepted: the agent has changed, so a sibling that restored nothing (or
    #: less) before is a different candidate now and gets to be screened again. `accepted_ids`
    #: is the part that never expires — a patch already in the stack is never re-applied.
    tried_ids: set[str] = set()
    accepted_ids: set[str] = set()
    final_verify_run_id: str | None = None
    selection_runs: list[str] = []
    collateral_checks = 0
    refused: set[str] = set()
    log.extend(thinking_refusal_lines(per_case_sigs, unrestored, refused))

    adjudication_skipped: list[str] = []
    cost_stopped = False

    def is_pass(k: int, n: int) -> bool:
        return outcome(k, n, thresholds["pass"], thresholds["fail"]) == OUTCOME_PASS

    while unrestored and tried < budget:
        candidates = [
            p
            for p in generate_candidates(work_dir, _ordered_signatures(per_case_sigs))
            if p.id not in tried_ids
        ]
        if not candidates:
            log.append("no further candidates for the observed failure signatures; giving up")
            break

        # --- Screen EVERY sibling before verifying any of them ----------------------
        # A screen is N reps of the still-broken cases only, which is the cheapest honest
        # measurement of "does this candidate fix anything". Doing all of them first is what
        # makes the choice between siblings a comparison rather than an accident of ordering.
        still_broken = set(unrestored)
        screened: list[_Screened] = []
        for order, patch in enumerate(candidates):
            if tried >= budget:
                log.append(
                    f"repair budget reached: {len(candidates) - order} further candidate(s) "
                    f"for this signature round were not screened"
                )
                break
            # Between candidates: a ceiling that was reached by the last run stops the loop
            # here rather than after another screen has been paid for.
            if cost_ceiling is not None:
                cost_ceiling.check(f"repair candidate {tried + 1}/{budget}")
            tried += 1
            tried_ids.add(patch.id)
            log.append(f"candidate {tried}/{budget}: [{patch.repair_type}] {patch.id} — "
                       f"{patch.description}")
            with _trial(work_dir, patch) as trial_dir:
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
                    case_ids=sorted(still_broken),
                    workers=workers,
                    notes=f"repair screen for candidate {patch.id}",
                    **execution,
                    cost_ceiling=cost_ceiling,
                )
            selection_runs.append(screen_id)
            screen_counts = _case_pass_counts(
                recorder.run_dir(runs_root, screen_id), sorted(still_broken)
            )
            screen_restored = {c for c, (k, n) in screen_counts.items() if is_pass(k, n)}
            screened.append(
                _Screened(
                    patch=patch,
                    index=tried,
                    cfg=cfg,
                    screen_id=screen_id,
                    screen_counts=screen_counts,
                    restored=screen_restored,
                    order=order,
                )
            )
            log.append(
                f"  screen {patch.id}: {len(screen_restored)}/{len(still_broken)} broken "
                f"cases restored"
            )

        ranked = rank_screened(screened, still_broken)
        if not ranked:
            log.append(
                "  no screened candidate restored a broken case — none earned a full "
                "verification run"
            )
            if tried >= budget:
                log.append(f"repair budget exhausted ({budget} candidates tried)")
            else:
                log.append("all current candidates rejected; giving up")
            break
        if len(ranked) > 1:
            log.append(
                "  verification order (most restored, then no disclosed change, then "
                "playbook rank): "
                + ", ".join(
                    f"{s.patch.id} ({len(s.restored)}/{len(still_broken)}"
                    + (", discloses a changed guarantee or cost"
                       if disclosures_for(s.patch.id) else "")
                    + ")"
                    for s in ranked
                )
            )

        # --- Verify the best; on rejection, fall to the next ------------------------
        progressed = False
        for choice in ranked:
            patch = choice.patch
            if cost_ceiling is not None:
                cost_ceiling.check(f"full verification of candidate {patch.id}")
            with _trial(work_dir, patch) as trial_dir:
                verify_id = f"{run_prefix}-c{choice.index:02d}-{choice.cfg}-verify"
                run_suite(
                    trial_dir,
                    provider,
                    verify_id,
                    n_reps=n_reps,
                    model_override=candidate_model,
                    runs_root=runs_root,
                    workers=workers,
                    notes=f"repair full verification for candidate {patch.id}",
                    **execution,
                    cost_ceiling=cost_ceiling,
                )
                selection_runs.append(verify_id)
                # Every protected case is re-measured by this run: that is what a collateral
                # check IS, and counting them is what makes "zero collateral damage" a
                # measurement instead of a slogan. With nothing protected the count stays 0
                # and the report says so (rescue-ops ghi56-006, ghisdk-052, ghc-223: "repair
                # start: N regressed case(s), 0 protected passing case(s)").
                collateral_checks += len(protected)
                verify_counts = _case_pass_counts(
                    recorder.run_dir(runs_root, verify_id), all_case_ids
                )

                # Restoration claims must survive screen AND verify combined (2N reps on
                # the same config) — a lucky single-run pass does not count as restored, and
                # neither do two flaky-band samples that only look like one pass together
                # (rescue-ops ghisdk-052 hunk 2: 2/5 + 3/5 = 5/10, below the 0.8 threshold).
                newly_restored = set()
                for case_id in still_broken:
                    sk, sn = choice.screen_counts[case_id]
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
                    adj_id = f"{run_prefix}-c{choice.index:02d}-{choice.cfg}-adj"
                    try:
                        run_suite(
                            trial_dir,
                            provider,
                            adj_id,
                            n_reps=n_reps,
                            model_override=candidate_model,
                            runs_root=runs_root,
                            case_ids=suspects,
                            workers=workers,
                            notes=(
                                f"adjudication of contested cases for candidate {patch.id}"
                            ),
                            **execution,
                            cost_ceiling=cost_ceiling,
                        )
                    except CostCeilingExceeded:
                        # DESIGN.md §D: adjudication cannot be SKIPPED. Accepting here would
                        # ship a patch whose contested cases were never measured and publish
                        # SAFE WITH PATCH on the strength of the sample that raised the
                        # suspicion (rescue-ops ghisdk-052: hunk 2's 2/5 and 3/5 were never
                        # adjudicated because the run went over budget). The candidate is
                        # rejected and the run ends INCONCLUSIVE(cost_ceiling).
                        adjudication_skipped = list(suspects)
                        cost_stopped = True
                        log.append(
                            f"  REJECTED: the cost ceiling stopped the adjudication of "
                            f"contested case(s) {suspects}. A candidate is never accepted on "
                            f"an unadjudicated sample; the verdict is INCONCLUSIVE"
                        )
                        break
                    selection_runs.append(adj_id)
                    adj_counts = _case_pass_counts(recorder.run_dir(runs_root, adj_id), suspects)
                    for case_id in suspects:
                        vk, vn = verify_counts[case_id]
                        ak, an = adj_counts[case_id]
                        contested = "cleared" if is_pass(vk + ak, vn + an) else "CONFIRMED"
                        log.append(
                            f"  adjudication {case_id}: verify {vk}/{vn} + extra {ak}/{an} "
                            f"= {vk + ak}/{vn + an} — {contested}"
                        )
                        if contested == "CONFIRMED":
                            confirmed_bad.append(case_id)
                elif suspects:
                    confirmed_bad = suspects  # nothing restored anyway; no need to spend

                broken = sorted(c for c in confirmed_bad if c in protected)
                relapsed = sorted(c for c in confirmed_bad if c in restored)

                if newly_restored and not broken and not relapsed:
                    _copy_agent_dir(trial_dir, work_dir)
                    accepted.append(patch)
                    accepted_ids.add(patch.id)
                    # The stacked agent is different now: every sibling this round screened
                    # and did not accept becomes eligible again, because what it does on top
                    # of this patch is not what it did without it. Re-screening costs budget,
                    # which is the honest price and is what `budget` is there to bound.
                    tried_ids = set(accepted_ids)
                    restored |= newly_restored
                    unrestored -= newly_restored
                    final_verify_run_id = verify_id
                    disclosures = disclosures_for(patch.id)
                    log.append(
                        f"  ACCEPTED {patch.id}: restored {sorted(newly_restored)}; "
                        f"0 previously-passing cases broken; "
                        f"{len(unrestored)} regressed case(s) remain"
                    )
                    for sentence in disclosures:
                        log.append(f"    DISCLOSURE {patch.id}: {sentence}")
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
                    reasons.append(
                        "verification restored no broken case (the screen's restoration did "
                        "not hold over the combined 2N reps)"
                    )
                if broken:
                    reasons.append(f"broke previously-passing case(s) {broken}")
                if relapsed:
                    reasons.append(f"relapsed earlier-restored case(s) {relapsed}")
                log.append(f"  REJECTED {patch.id}: {'; '.join(reasons)}")
        if cost_stopped:
            break
        if not progressed:
            if tried >= budget:
                log.append(f"repair budget exhausted ({budget} candidates tried)")
            else:
                log.append("all current candidates rejected; giving up")
            break

    # --- Fresh final verification (DESIGN.md §D) ------------------------------------
    # The runs above SELECTED the candidates; a candidate picked because it won on one
    # sample cannot also be confirmed by that sample. One more full-suite run of the
    # stacked patch, at seeds no selection run used, is what the verdict rests on.
    final_run_id: str | None = None
    broken_by_patch: list[str] = []
    unconfirmed: list[str] = []
    evidence_label = "selection_evidence_only"
    if accepted and final_verify and not cost_stopped:
        final_run_id = f"{run_prefix}-final"
        with _salted_seeds(run_suite, FINAL_RUN_SEED_SALT) as seed_kwargs:
            run_suite(
                work_dir,
                provider,
                final_run_id,
                n_reps=n_reps,
                model_override=candidate_model,
                runs_root=runs_root,
                workers=workers,
                notes=(
                    "fresh final verification of the stacked patch at new seeds; the verdict "
                    "rests on this run, not on the screen/verify runs that selected it"
                ),
                cost_ceiling=cost_ceiling,
                **execution,
                **seed_kwargs,
            )
        final_counts = _case_pass_counts(recorder.run_dir(runs_root, final_run_id), all_case_ids)

        def final_pass(case_id: str) -> bool:
            k, n = final_counts[case_id]
            return outcome(k, n, thresholds["pass"], thresholds["fail"]) == OUTCOME_PASS

        collateral_checks += len(protected)
        broken_by_patch = sorted(c for c in protected if not final_pass(c))
        unconfirmed = sorted(c for c in restored if not final_pass(c))
        evidence_label = "fresh_final_verification"
        log.append(
            f"final verification {final_run_id}: full suite, {n_reps} reps, fresh seeds — "
            f"{sum(1 for c in all_case_ids if final_pass(c))}/{len(all_case_ids)} cases pass"
        )
        if unconfirmed:
            # Selection said restored; the fresh sample says otherwise. The fresh sample wins,
            # and the case goes back to unrestored, which is a STAY PINNED.
            log.append(
                f"  NOT CONFIRMED by the fresh final run: {unconfirmed} — selection evidence "
                f"is not verification evidence; these cases return to unrestored"
            )
            restored -= set(unconfirmed)
            unrestored |= set(unconfirmed)
        if broken_by_patch:
            log.append(
                f"  COLLATERAL DAMAGE on the fresh final run: {broken_by_patch} passed on the "
                f"candidate before the patch and fail after it"
            )
    elif accepted and cost_stopped:
        log.append(
            "final verification SKIPPED: the cost ceiling stopped this run mid-decision. The "
            "patches already accepted stand on their selection runs, and the verdict is "
            "INCONCLUSIVE (cost_ceiling) — never SAFE WITH PATCH"
        )
    elif accepted and not final_verify:
        log.append(
            "final verification SKIPPED (final_verify=False): the verdict below rests on the "
            "runs that selected the candidates, which is weaker evidence — labelled "
            "selection_evidence_only"
        )
    elif not accepted:
        evidence_label = None

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
        final_run_id=final_run_id,
        selection_runs=selection_runs,
        broken_by_patch=broken_by_patch,
        unconfirmed_by_final=unconfirmed,
        protected_cases=list(protected),
        collateral_checks=collateral_checks,
        evidence_label=evidence_label,
        adjudication_skipped=adjudication_skipped,
        cost_stopped=cost_stopped,
    )
