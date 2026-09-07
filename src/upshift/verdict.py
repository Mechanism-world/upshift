"""Final verdict: SAFE / SAFE WITH PATCH / STAY PINNED / BASELINE_BROKEN / INCONCLUSIVE.

The verdict is deliberately conservative:
- INCONCLUSIVE(empty_suite) comes first: with no cases there is nothing to be broken.
- BASELINE_BROKEN is next and is terminal: when the baseline model passed no case at all,
  nothing about the candidate was measured, and every other verdict would be a claim the
  evidence does not support.
- Every other INCONCLUSIVE reason (DESIGN.md §D) is checked before any pass/fail verdict:
  an incomplete run, a billing or auth failure, a harness/runner failure, an unavailable
  model, a cost-ceiling stop, a missing run, or evidence that mixes a simulator with a real
  provider. None of them can produce SAFE or SAFE WITH PATCH.
- SAFE requires zero regressed cases. Flaky degradations do not block, but are listed.
- SAFE WITH PATCH requires every regressed case restored and zero previously-passing cases
  broken, proven by a FRESH full-suite verification run of the patched agent that is not one
  of the runs that selected the candidates.
- Anything less is STAY PINNED.

The distinction the reason codes exist to protect: a 400 that the candidate model returns
because it rejects the request the agent sends is BEHAVIOUR — it is exactly the regression
upshift is built to find. A 400 because the account is out of credit, the key is wrong, the
harness crashed or the recording ran out of turns is NOT behaviour, and counting it as one
manufactures a regression out of an operational accident.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from upshift import recorder, stats
from upshift.differ import DiffResult
from upshift.repair.loop import RepairOutcome
from upshift.report import REAL_PROVIDERS
from upshift.schemas import LABEL_FLAKY, LABEL_IMPROVED, LABEL_REGRESSED, OUTCOME_PASS

SAFE = "SAFE"
SAFE_WITH_PATCH = "SAFE WITH PATCH"
STAY_PINNED = "STAY PINNED"
#: The baseline model passed no case, so the suite measured nothing. Terminal: it is a fact
#: about the agent directory or the eval suite, not about the candidate model, and no repair
#: the loop can generate would change it. (rescue-ops LAB_RUNBOOK already treats it as one.)
BASELINE_BROKEN = "BASELINE_BROKEN"
#: The run cannot support ANY claim about the upgrade. Always reason-coded (DESIGN.md §D).
INCONCLUSIVE = "INCONCLUSIVE"

#: The CLI exit code for INCONCLUSIVE. Deliberately the SAME value as the existing
#: `cli.EXIT_COST_STOPPED`, because they are the same claim: the pipeline reached no
#: conclusion. 0 = SAFE / SAFE WITH PATCH, 1 = STAY PINNED / BASELINE_BROKEN, 2 = a usage or
#: API error, 3 = no conclusion. A caller that must distinguish a cost stop from another
#: inconclusive reason reads `verdict.json`'s `reasons`, not the exit code.
EXIT_INCONCLUSIVE = 3

# --- INCONCLUSIVE reason codes -------------------------------------------------------
REASON_EMPTY_SUITE = "empty_suite"
REASON_INCOMPLETE_RUN = "incomplete_run"
REASON_BILLING_ERROR = "billing_error"
REASON_AUTH_ERROR = "auth_error"
REASON_RUNNER_ERROR = "runner_error"
REASON_CONTINUATION_EXHAUSTED = "continuation_exhausted"
REASON_COST_CEILING = "cost_ceiling"
REASON_MISSING_RUN = "missing_run"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_MIXED_EVIDENCE = "mixed_evidence"

INCONCLUSIVE_REASONS = (
    REASON_EMPTY_SUITE,
    REASON_INCOMPLETE_RUN,
    REASON_BILLING_ERROR,
    REASON_AUTH_ERROR,
    REASON_RUNNER_ERROR,
    REASON_CONTINUATION_EXHAUSTED,
    REASON_COST_CEILING,
    REASON_MISSING_RUN,
    REASON_MODEL_UNAVAILABLE,
    REASON_MIXED_EVIDENCE,
)

#: One line per reason, printed by the report so the reader does not have to look the code up.
REASON_DESCRIPTIONS = {
    REASON_EMPTY_SUITE: "the eval suite contains no cases, so nothing was measured.",
    REASON_INCOMPLETE_RUN: (
        "at least one case has fewer recorded reps than the run's own n_reps: the pass rates "
        "are not the rates the thresholds were set for."
    ),
    REASON_BILLING_ERROR: (
        "a recorded failure is a billing/quota refusal, which is a fact about the account, "
        "not about the model."
    ),
    REASON_AUTH_ERROR: (
        "a recorded failure is an authentication/permission refusal, which is a fact about "
        "the key, not about the model."
    ),
    REASON_RUNNER_ERROR: (
        "a recorded failure came from the harness or the runner (a crash, a timeout, a "
        "malformed result), not from the model."
    ),
    REASON_CONTINUATION_EXHAUSTED: (
        "a capture-derived episode needed more assistant turns than the recording provided "
        "(DESIGN.md §F); the episode ended for want of a recording, not for want of a model."
    ),
    REASON_COST_CEILING: "verification was stopped by the cost ceiling before it finished.",
    REASON_MISSING_RUN: "a run the verdict would have to rest on is not on disk.",
    REASON_MODEL_UNAVAILABLE: (
        "the provider reports the candidate model does not exist or is not available to this "
        "account; the upgrade was never exercised."
    ),
    REASON_MIXED_EVIDENCE: (
        "the runs disagree on provider realness: simulator output and live output never mix, "
        "because one of them costs money and means something and the other does not."
    ),
}

# --- Non-behavioural failure classification ------------------------------------------

#: Copied, deliberately, from runner.BILLING_ERROR_RE rather than imported: runner.py is the
#: execution path and this is the adjudication path, and a verdict must be derivable from run
#: records alone without importing the machinery that produced them. Keep the two in sync.
BILLING_ERROR_RE = re.compile(
    r"credit balance|insufficient_quota|exceeded your current quota|billing|"
    r"purchase credits|payment required",
    re.IGNORECASE,
)

AUTH_ERROR_RE = re.compile(
    r"invalid[ _]api[ _]key|incorrect api key|authentication|unauthorized|"
    r"invalid[ _]x-api-key|permission denied|not allowed to access|forbidden",
    re.IGNORECASE,
)

MODEL_UNAVAILABLE_RE = re.compile(
    r"model[^.]{0,40}(does not exist|not found|is not available|unknown model)|"
    r"the model .* does not exist",
    re.IGNORECASE,
)

#: `error_type` strings other streams record for failures that are NOT the model's behaviour,
#: mapped onto the reason each one produces. Matched defensively on the string: the constants
#: live in the modules that raise them (capture continuation, the native runner) and this
#: module must keep classifying correctly whether or not those modules are importable here.
NON_BEHAVIOURAL_ERROR_TYPES = {
    "sdk_validation": REASON_RUNNER_ERROR,
    "harness_error": REASON_RUNNER_ERROR,
    "runner_error": REASON_RUNNER_ERROR,
    "continuation_exhausted": REASON_CONTINUATION_EXHAUSTED,
    "billing_error": REASON_BILLING_ERROR,
    "auth_error": REASON_AUTH_ERROR,
    "authentication_error": REASON_AUTH_ERROR,
    "permission_error": REASON_AUTH_ERROR,
    "model_unavailable": REASON_MODEL_UNAVAILABLE,
    "not_found_error": REASON_MODEL_UNAVAILABLE,
    "cost_ceiling": REASON_COST_CEILING,
}


def classify_api_error(api_error: Any) -> str | None:
    """The INCONCLUSIVE reason one recorded ``api_error`` implies, or None if it is BEHAVIOUR.

    None is the important return value: a 400 the model returns because it will not accept
    the request the agent sends is precisely the regression upshift exists to detect, and
    must never be laundered into "inconclusive". Only operational failures are reclassified.
    """
    if not isinstance(api_error, dict):
        return None
    error_type = str(api_error.get("type") or "").strip().lower()
    if error_type in NON_BEHAVIOURAL_ERROR_TYPES:
        return NON_BEHAVIOURAL_ERROR_TYPES[error_type]
    message = str(api_error.get("message") or "")
    status = api_error.get("status_code")
    if BILLING_ERROR_RE.search(message):
        return REASON_BILLING_ERROR
    if status in (401, 403) or AUTH_ERROR_RE.search(message):
        return REASON_AUTH_ERROR
    if MODEL_UNAVAILABLE_RE.search(message):
        return REASON_MODEL_UNAVAILABLE
    return None


def scan_run_for_non_behavioural(run_directory: str | Path) -> dict[str, list[str]]:
    """{reason: [case ids]} for every rep in a run whose failure was not the model's.

    Reads the rep records directly, the same way the differ does, because ``summary.json``
    is a convenience and the verdict may not rest on a convenience.
    """
    run_directory = Path(run_directory)
    found: dict[str, set[str]] = {}
    cases_dir = run_directory / "cases"
    if not cases_dir.is_dir():
        return {}
    for case_dir in sorted(cases_dir.iterdir()):
        if not case_dir.is_dir():
            continue
        for path in sorted(case_dir.glob("rep_*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                found.setdefault(REASON_RUNNER_ERROR, set()).add(case_dir.name)
                continue
            reason = classify_api_error(data.get("api_error"))
            if reason:
                found.setdefault(reason, set()).add(case_dir.name)
    return {reason: sorted(cases) for reason, cases in sorted(found.items())}


# --- Evidence integrity ---------------------------------------------------------------


def _is_real(provider: Any) -> bool:
    return str(provider) in REAL_PROVIDERS


def _incomplete_cases(diff: DiffResult) -> list[str]:
    """Cases with fewer reps than the manifest's own n_reps, in either run.

    A short case is not a slightly noisier case: the pass/fail thresholds are fractions of
    n_reps, so 3 of 5 reps recorded turns "3/5, flaky" into "3/3, pass" with no warning.
    """
    out = []
    for run_manifest, attr in (
        (diff.baseline_manifest, "baseline_n"),
        (diff.candidate_manifest, "candidate_n"),
    ):
        expected = run_manifest.get("n_reps")
        if not isinstance(expected, int) or expected <= 0:
            continue
        for case in diff.cases:
            if getattr(case, attr) < expected:
                out.append(case.case_id)
    return sorted(set(out))


def evidence_integrity(
    diff: DiffResult,
    *,
    runs_root: str | Path | None = None,
    extra_run_ids: list[str] | None = None,
    cost_stopped: bool = False,
) -> dict[str, dict[str, Any]]:
    """{reason: {"detail": str, "cases": [...]}}: every §D reason this evidence triggers.

    ``runs_root`` is optional and its absence is honest rather than fatal: without it the
    checks that need rep records (the non-behavioural failure classes) are skipped and the
    ones derivable from the diff alone (empty suite, short runs, mixed providers) still run.
    ``extra_run_ids`` are runs the verdict also rests on — the fresh final verification, and
    the selection runs behind it.
    """
    issues: dict[str, dict[str, Any]] = {}

    def add(reason: str, detail: str, cases: list[str] | None = None) -> None:
        entry = issues.setdefault(reason, {"detail": detail, "cases": []})
        entry["cases"] = sorted(set(entry["cases"]) | set(cases or []))

    if not diff.cases:
        add(REASON_EMPTY_SUITE, "the diff contains no cases")

    short = _incomplete_cases(diff)
    if short:
        add(
            REASON_INCOMPLETE_RUN,
            f"{len(short)} case(s) have fewer reps than the run's n_reps",
            short,
        )

    baseline_provider = diff.baseline_manifest.get("provider")
    candidate_provider = diff.candidate_manifest.get("provider")
    if _is_real(baseline_provider) != _is_real(candidate_provider):
        add(
            REASON_MIXED_EVIDENCE,
            f"baseline provider {baseline_provider!r} and candidate provider "
            f"{candidate_provider!r} disagree on whether a real model was called",
        )

    if cost_stopped:
        add(REASON_COST_CEILING, "the pipeline was stopped by its cost ceiling")

    if runs_root is not None:
        run_ids = [diff.baseline_run_id, diff.candidate_run_id, *(extra_run_ids or [])]
        for run_id in [r for r in run_ids if r]:
            directory = Path(runs_root) / str(run_id)
            if not (directory / "cases").is_dir():
                add(REASON_MISSING_RUN, f"run {run_id!r} is not on disk under {runs_root}")
                continue
            for reason, cases in scan_run_for_non_behavioural(directory).items():
                add(
                    reason,
                    f"recorded in run {run_id!r}",
                    [f"{run_id}:{case_id}" for case_id in cases],
                )
    return issues


# --- Statistics wording ----------------------------------------------------------------


def detectable_effect(n_reps: Any, alpha: float = 0.05) -> str:
    """The §D sentence: the smallest per-case degradation this N could have detected.

    Written out because "no significant difference" at N=5 is a statement about the power of
    five reps, not about the models, and a reader who is not told the difference will read
    the first as the second.
    """
    try:
        n = int(n_reps)
    except (TypeError, ValueError):
        return (
            "the number of reps is not recorded, so the smallest detectable effect cannot "
            "be stated; a non-significant difference is not evidence of equivalence."
        )
    drop = stats.smallest_detectable_drop(n, alpha=alpha)
    if drop is None:
        return (
            f"at N={n} reps per case, NO per-case degradation reaches p<{alpha:g} on a "
            f"one-sided Fisher exact test — this run could not have detected even a total "
            f"collapse. A non-significant difference is not evidence of equivalence."
        )
    passes, p_value = drop
    return (
        f"at N={n} reps per case, the smallest degradation detectable at p<{alpha:g} is "
        f"{n}/{n} -> {passes}/{n} (p={p_value:.3g}); anything milder cannot reach "
        f"significance at this N. A non-significant difference is not evidence of "
        f"equivalence — it is the absence of evidence of a difference."
    )


# --- Verification scope ------------------------------------------------------------------


def scope_of(diff: DiffResult) -> str:
    """The run's §A verification scope, from the candidate manifest, defaulting to the
    meaning every pre-v0.5 run had."""
    return str(diff.candidate_manifest.get("scope") or recorder.SCOPE_ADAPTED_AGENT)


def _evidence_ids(diff: DiffResult) -> dict[str, str | None]:
    return {
        diff.baseline_run_id: diff.baseline_manifest.get("evidence_id"),
        diff.candidate_run_id: diff.candidate_manifest.get("evidence_id"),
    }


# --- The verdict -------------------------------------------------------------------------


def decide(
    diff: DiffResult,
    repair_outcome: RepairOutcome | None = None,
    patch_path: str | None = None,
    framework: str | None = None,
    *,
    runs_root: str | Path | None = None,
    cost_stopped: bool = False,
) -> dict[str, Any]:
    """``framework`` is the framework a capture-derived agent directory was built from
    (``upshift.capture.mapping.framework_of``). It is carried in the verdict so a report
    rendered later, from verdict.json alone, still knows where each repair lives.

    ``runs_root``, when given, lets the verdict read the rep records and classify
    non-behavioural failures (DESIGN.md §D). Without it those checks are skipped and the
    verdict says so in ``integrity_checked``, rather than implying a clean bill of health it
    never looked for.
    """
    regressed = sorted(c.case_id for c in diff.cases if c.label == LABEL_REGRESSED)
    flaky = sorted(c.case_id for c in diff.cases if c.label == LABEL_FLAKY)
    improved = sorted(c.case_id for c in diff.cases if c.label == LABEL_IMPROVED)
    baseline_passing = diff.baseline_passing_cases()
    protected = sorted(c.case_id for c in diff.cases if c.candidate_outcome == OUTCOME_PASS)

    extra_runs: list[str] = []
    if repair_outcome is not None:
        extra_runs = [
            r
            for r in [
                getattr(repair_outcome, "final_run_id", None),
                *(getattr(repair_outcome, "selection_runs", None) or []),
            ]
            if r
        ]
    # The repair loop reports its own cost stop, and it must be honoured even when the caller
    # did not see the exception: the loop CATCHES `CostCeilingExceeded` around an adjudication
    # run so that a stopped pipeline still produces a verdict, and the verdict it produces has
    # to be INCONCLUSIVE. A candidate whose contested cases were never adjudicated is not a
    # candidate that passed (DESIGN.md §D; rescue-ops ghisdk-052).
    cost_stopped = cost_stopped or bool(getattr(repair_outcome, "cost_stopped", False))
    issues = evidence_integrity(
        diff, runs_root=runs_root, extra_run_ids=extra_runs, cost_stopped=cost_stopped
    )

    restored: list[str] = []
    unrestored: list[str] = []
    repair_log: list[str] = repair_outcome.log if repair_outcome else []
    broken_cases = list(getattr(repair_outcome, "broken_by_patch", None) or [])
    reasons: list[str] = []

    if REASON_EMPTY_SUITE in issues:
        # Before BASELINE_BROKEN: with no cases the baseline "passed 0 of 0", which is not a
        # broken baseline, it is an absent suite.
        verdict = INCONCLUSIVE
        reasons = [REASON_EMPTY_SUITE]
        repair_log = []
    elif not baseline_passing:
        # Checked BEFORE `regressed`, and it cannot hide one: a regression requires a case
        # the baseline passed, so `regressed` is necessarily empty here and the verdict this
        # replaces was always the vacuous SAFE.
        verdict = BASELINE_BROKEN
        repair_log = []
    elif issues:
        verdict = INCONCLUSIVE
        reasons = [r for r in INCONCLUSIVE_REASONS if r in issues]
        restored = repair_outcome.restored if repair_outcome else []
        unrestored = repair_outcome.unrestored if repair_outcome else regressed
    elif not regressed:
        verdict = SAFE
        repair_log = []
    elif repair_outcome is not None and not repair_outcome.unrestored and not broken_cases:
        verdict = SAFE_WITH_PATCH
        restored = repair_outcome.restored
    else:
        verdict = STAY_PINNED
        restored = repair_outcome.restored if repair_outcome else []
        unrestored = repair_outcome.unrestored if repair_outcome else regressed

    collateral = {
        "protected_cases": len(protected),
        "checks_executed": int(getattr(repair_outcome, "collateral_checks", 0) or 0),
        "exercised": bool(protected) and bool(getattr(repair_outcome, "collateral_checks", 0)),
    }

    final_run_id = getattr(repair_outcome, "final_run_id", None) if repair_outcome else None
    evidence_label = (
        getattr(repair_outcome, "evidence_label", None) if repair_outcome else None
    ) or ("fresh_final_verification" if final_run_id else None)

    return {
        "verdict": verdict,
        "reasons": reasons,
        # The single reason code a reader (or a script) asks for first. `reasons` stays the
        # authority — an INCONCLUSIVE run can have several — this is its first entry, and
        # None when the verdict is not INCONCLUSIVE, so `if v["inconclusive_reason"]` reads
        # as "was this run inconclusive, and why".
        "inconclusive_reason": reasons[0] if reasons else None,
        "reason_details": {r: issues[r] for r in reasons} if reasons else {},
        "integrity_checked": runs_root is not None,
        "scope": scope_of(diff),
        "provider": diff.candidate_manifest.get("provider"),
        "baseline_model": diff.baseline_manifest["agent"]["model_requested"],
        "candidate_model": diff.candidate_manifest["agent"]["model_requested"],
        "baseline_passing_cases": baseline_passing,
        "cases_total": len(diff.cases),
        "regressed_total": len(regressed),
        "regressed": regressed,
        "restored": len(restored),
        "restored_cases": restored,
        "unrestored": unrestored,
        # No longer zero by construction: the FRESH final verification (DESIGN.md §D) can
        # break a case the selection runs said was safe, and when it does it is reported.
        "broken_by_patch": len(broken_cases),
        "broken_by_patch_cases": broken_cases,
        "collateral": collateral,
        # Suspect cases a candidate's adjudication run could not measure. Non-empty means a
        # candidate was rejected for lack of evidence rather than for failing, which is a
        # different thing for a reader deciding whether to rerun with a higher ceiling.
        "adjudication_skipped": list(
            getattr(repair_outcome, "adjudication_skipped", None) or []
        ),
        "flaky": flaky,
        "improved": improved,
        "patch_path": patch_path if verdict == SAFE_WITH_PATCH else None,
        "repair_log": repair_log,
        "framework": framework,
        "n_reps": diff.candidate_manifest.get("n_reps"),
        "detectable_effect": detectable_effect(diff.candidate_manifest.get("n_reps")),
        "evidence_ids": _evidence_ids(diff),
        "final_run_id": final_run_id,
        "selection_runs": list(getattr(repair_outcome, "selection_runs", None) or [])
        if repair_outcome
        else [],
        "evidence_label": evidence_label,
        # The accepted repairs, structured. `repair_log` is prose for a human to read; this is
        # what the report's framework mapping is keyed on (upshift.capture.mapping).
        "accepted_patches": [
            {"id": p.id, "repair_type": p.repair_type, "description": p.description}
            for p in (repair_outcome.accepted_patches if repair_outcome else [])
        ],
    }
