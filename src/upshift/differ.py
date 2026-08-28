"""Behavioral diff between two run records on disk.

The differ is deliberately paranoid: it RECOMPUTES every pass count from the individual
``rep_*.json`` files and never trusts ``summary.json``, because the diff is the evidence
behind the verdict. It also refuses to compare runs that are not comparable (different case
sets, different thresholds, different n_reps) rather than silently producing a number.

On-disk layout (contract with the runner)::

    runs/<run_id>/manifest.json
    runs/<run_id>/cases/<case_id>/rep_<k>.json   # dataclasses.asdict(RepRecord)
    runs/<run_id>/summary.json                   # convenience only, never read here
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from upshift import stats
from upshift.schemas import LABEL_STABLE_PASS, RepRecord, label, outcome

# ---------------------------------------------------------------------------
# Failure signature taxonomy (contract with repair/playbook.py — keep in sync)
# ---------------------------------------------------------------------------

SIG_API_ERROR_TOOLS_REASONING = "api_error_tools_reasoning"
SIG_API_ERROR_OTHER = "api_error_other"
SIG_DUPLICATE_TOOL_CALLS = "duplicate_tool_calls"
SIG_ACTING_PAST_GOAL = "acting_past_goal"
SIG_SKIPPED_TOOL_HALLUCINATION = "skipped_tool_hallucination"
SIG_WRONG_OR_MISSING_TOOL_CALL = "wrong_or_missing_tool_call"
SIG_OTHER_BEHAVIORAL = "other_behavioral"

#: Report/playbook ordering. Signatures are always emitted in this order.
SIGNATURE_PRIORITY = (
    SIG_API_ERROR_TOOLS_REASONING,
    SIG_API_ERROR_OTHER,
    SIG_DUPLICATE_TOOL_CALLS,
    SIG_ACTING_PAST_GOAL,
    SIG_SKIPPED_TOOL_HALLUCINATION,
    SIG_WRONG_OR_MISSING_TOOL_CALL,
    SIG_OTHER_BEHAVIORAL,
)

_RE_FUNCTION_TOOLS = re.compile(r"function tools", re.IGNORECASE)
_RE_REASONING_EFFORT = re.compile(r"reasoning[_ ]effort", re.IGNORECASE)
_RE_CONFIRMATION_ID = re.compile(r"UPS-\d+")

MAX_FAILING_DETAILS = 5


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CaseDiff:
    """One case, both runs, with the label and the evidence for it."""

    case_id: str
    baseline_passes: int
    baseline_n: int
    candidate_passes: int
    candidate_n: int
    baseline_outcome: str
    candidate_outcome: str
    label: str
    p_value: float
    failure_signatures: list[str]
    failing_check_details: list[str]


@dataclass
class DiffResult:
    baseline_run_id: str
    candidate_run_id: str
    baseline_manifest: dict[str, Any]
    candidate_manifest: dict[str, Any]
    cases: list[CaseDiff]
    counts: dict[str, int]  # per label
    agent_files_differ: bool = False

    def by_label(self, wanted: str) -> list[CaseDiff]:
        return [c for c in self.cases if c.label == wanted]

    def non_stable_pass(self) -> list[CaseDiff]:
        return [c for c in self.cases if c.label != LABEL_STABLE_PASS]


# ---------------------------------------------------------------------------
# Failure signatures
# ---------------------------------------------------------------------------


def _check_type(check: dict[str, Any]) -> str:
    return str(check.get("type", ""))


def _failed_checks(record: RepRecord) -> list[dict[str, Any]]:
    return [cr.check for cr in record.check_results if not cr.passed]


def _api_error_matches_tools_reasoning(err: dict[str, Any]) -> bool:
    if err.get("status_code") != 400:
        return False
    message = str(err.get("message", ""))
    return bool(_RE_FUNCTION_TOOLS.search(message) and _RE_REASONING_EFFORT.search(message))


def _count_confirmed_bookings(final_state: dict[str, Any]) -> int | None:
    """Total confirmed bookings in a recorded backend state, or None if unreadable.

    Tolerant of the backend storing bookings as either a dict keyed by confirmation id or a
    list. An entry counts when it has no status field at all or its status is "confirmed"
    (cancelled bookings must not inflate the duplicate detector).
    """
    bookings = final_state.get("bookings")
    if isinstance(bookings, dict):
        items: list[Any] = list(bookings.values())
    elif isinstance(bookings, list):
        items = list(bookings)
    else:
        return None
    count = 0
    for item in items:
        if isinstance(item, dict):
            status = item.get("status")
            if status is None or str(status).lower() == "confirmed":
                count += 1
        else:
            count += 1
    return count


def _has_repeated_tool_call(record: RepRecord) -> bool:
    """A (name, arguments) pair executed more than once, successfully, within one segment."""
    seen: set[tuple[int, str, str]] = set()
    for ex in record.tool_executions:
        result = ex.result
        if isinstance(result, dict) and "error" in result:
            continue
        try:
            args = json.dumps(ex.arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = repr(ex.arguments)
        key = (ex.segment, ex.name, args)
        if key in seen:
            return True
        seen.add(key)
    return False


def _has_excess_bookings(record: RepRecord) -> bool:
    """A failed bookings_count check whose recomputed actual count exceeds `equals`.

    The check's detail string is not parsed (that would be fragile); the actual count is
    recomputed from ``record.final_state``. Only an *excess* counts as a duplicate signature —
    booking too few is a missing call, not a duplicated one.
    """
    for check in _failed_checks(record):
        if _check_type(check) != "bookings_count":
            continue
        expected = check.get("equals")
        actual = _count_confirmed_bookings(record.final_state)
        if isinstance(expected, int) and actual is not None and actual > expected:
            return True
    return False


def _is_hallucination_tool_check(record: RepRecord, check: dict[str, Any]) -> bool:
    """A failed `tool_called book_flight` while the final message quotes a UPS-<n> id."""
    return (
        _check_type(check) == "tool_called"
        and check.get("name") == "book_flight"
        and bool(_RE_CONFIRMATION_ID.search(record.final_message or ""))
    )


def _record_signatures(record: RepRecord, failing: bool) -> set[str]:
    sigs: set[str] = set()

    err = record.api_error
    if err:
        if _api_error_matches_tools_reasoning(err):
            sigs.add(SIG_API_ERROR_TOOLS_REASONING)
        else:
            sigs.add(SIG_API_ERROR_OTHER)
    if not failing:
        return sigs

    if _has_repeated_tool_call(record) or _has_excess_bookings(record):
        sigs.add(SIG_DUPLICATE_TOOL_CALLS)

    for check in _failed_checks(record):
        ctype = _check_type(check)
        if ctype == "no_tool_calls_after_success":
            sigs.add(SIG_ACTING_PAST_GOAL)
        elif ctype == "confirmation_id_valid":
            sigs.add(SIG_SKIPPED_TOOL_HALLUCINATION)
        elif ctype in ("tool_called", "tool_not_called"):
            if _is_hallucination_tool_check(record, check):
                sigs.add(SIG_SKIPPED_TOOL_HALLUCINATION)
            else:
                sigs.add(SIG_WRONG_OR_MISSING_TOOL_CALL)

    if not sigs:
        sigs.add(SIG_OTHER_BEHAVIORAL)
    return sigs


def failure_signatures(records: list[RepRecord]) -> list[str]:
    """Classify a case's reps into the repair playbook's failure signature taxonomy.

    Pass the reps of ONE case from ONE run (the differ passes the candidate run's reps).
    Returns deduplicated signatures ordered by ``SIGNATURE_PRIORITY``; an empty list when no
    rep failed, since a passing case has nothing to repair.
    """
    failing = [r for r in records if not r.passed]
    if not failing:
        return []

    found: set[str] = set()
    for record in records:
        found |= _record_signatures(record, failing=not record.passed)
    return [sig for sig in SIGNATURE_PRIORITY if sig in found]


def failing_details(records: list[RepRecord], limit: int = MAX_FAILING_DETAILS) -> list[str]:
    """Up to `limit` distinct CheckResult.detail strings from failed checks in failing reps."""
    out: list[str] = []
    seen: set[str] = set()
    for record in records:
        if record.passed:
            continue
        for cr in record.check_results:
            if cr.passed:
                continue
            detail = (cr.detail or "").strip()
            if not detail or detail in seen:
                continue
            seen.add(detail)
            out.append(detail)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Loading run records
# ---------------------------------------------------------------------------


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise ValueError(f"no manifest.json in run dir {run_dir}")
    return json.loads(path.read_text())


def _case_ids(run_dir: Path) -> set[str]:
    cases_dir = run_dir / "cases"
    if not cases_dir.is_dir():
        return set()
    return {p.name for p in cases_dir.iterdir() if p.is_dir()}


def _load_reps(run_dir: Path, case_id: str) -> list[RepRecord]:
    case_dir = run_dir / "cases" / case_id
    if not case_dir.is_dir():
        return []
    records = []
    for path in sorted(case_dir.glob("rep_*.json")):
        records.append(RepRecord.from_dict(json.loads(path.read_text())))
    records.sort(key=lambda r: r.rep)
    return records


def _require_reps(run_dir: Path, run_id: str, case_id: str, role: str) -> list[RepRecord]:
    records = _load_reps(run_dir, case_id)
    if not records:
        raise ValueError(
            f"case {case_id!r} has no reps in the {role} run {run_id!r} ({run_dir}); "
            "the two runs cover different cases and cannot be compared"
        )
    return records


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


def diff_runs(baseline_dir: str | Path, candidate_dir: str | Path) -> DiffResult:
    """Diff two completed runs, recomputing every pass count from the rep files.

    Raises ValueError when the runs are not comparable: different ``case_set_hash``,
    different thresholds, different ``n_reps``, or a case present in one run and missing (or
    empty) in the other. Differing agent ``file_hashes`` is NOT an error — that is exactly
    what a patched run looks like — it only sets ``DiffResult.agent_files_differ``.
    """
    baseline_dir = Path(baseline_dir)
    candidate_dir = Path(candidate_dir)
    b_manifest = _load_manifest(baseline_dir)
    c_manifest = _load_manifest(candidate_dir)
    b_run_id = str(b_manifest.get("run_id", baseline_dir.name))
    c_run_id = str(c_manifest.get("run_id", candidate_dir.name))

    if b_manifest.get("case_set_hash") != c_manifest.get("case_set_hash"):
        raise ValueError(
            f"case_set_hash differs between {b_run_id!r} and {c_run_id!r}; the two runs used "
            "different case sets and are not comparable"
        )
    b_thresholds = b_manifest.get("thresholds", {})
    if b_thresholds != c_manifest.get("thresholds", {}):
        raise ValueError(
            f"thresholds differ between {b_run_id!r} ({b_thresholds}) and {c_run_id!r} "
            f"({c_manifest.get('thresholds', {})}); outcomes would not be comparable"
        )
    if b_manifest.get("n_reps") != c_manifest.get("n_reps"):
        raise ValueError(
            f"n_reps differs between {b_run_id!r} ({b_manifest.get('n_reps')}) and "
            f"{c_run_id!r} ({c_manifest.get('n_reps')}); runs are not comparable"
        )

    pass_threshold = float(b_thresholds.get("pass", 0.8))
    fail_threshold = float(b_thresholds.get("fail", 0.4))

    case_ids = sorted(_case_ids(baseline_dir) | _case_ids(candidate_dir))
    if not case_ids:
        raise ValueError(f"neither {baseline_dir} nor {candidate_dir} contains any cases")

    cases: list[CaseDiff] = []
    counts: dict[str, int] = {}
    for case_id in case_ids:
        b_reps = _require_reps(baseline_dir, b_run_id, case_id, "baseline")
        c_reps = _require_reps(candidate_dir, c_run_id, case_id, "candidate")
        b_passes = sum(1 for r in b_reps if r.passed)
        c_passes = sum(1 for r in c_reps if r.passed)
        b_outcome = outcome(b_passes, len(b_reps), pass_threshold, fail_threshold)
        c_outcome = outcome(c_passes, len(c_reps), pass_threshold, fail_threshold)
        case_label = label(b_outcome, c_outcome)
        cases.append(
            CaseDiff(
                case_id=case_id,
                baseline_passes=b_passes,
                baseline_n=len(b_reps),
                candidate_passes=c_passes,
                candidate_n=len(c_reps),
                baseline_outcome=b_outcome,
                candidate_outcome=c_outcome,
                label=case_label,
                p_value=stats.fisher_exact_one_sided(b_passes, len(b_reps), c_passes, len(c_reps)),
                failure_signatures=failure_signatures(c_reps),
                failing_check_details=failing_details(c_reps),
            )
        )
        counts[case_label] = counts.get(case_label, 0) + 1

    b_hashes = (b_manifest.get("agent") or {}).get("file_hashes")
    c_hashes = (c_manifest.get("agent") or {}).get("file_hashes")

    return DiffResult(
        baseline_run_id=b_run_id,
        candidate_run_id=c_run_id,
        baseline_manifest=b_manifest,
        candidate_manifest=c_manifest,
        cases=cases,
        counts=counts,
        agent_files_differ=b_hashes != c_hashes,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_diff(result: DiffResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=1, sort_keys=True))
    return path


def load_diff(path: str | Path) -> DiffResult:
    raw = json.loads(Path(path).read_text())
    raw = dict(raw)
    raw["cases"] = [CaseDiff(**c) for c in raw["cases"]]
    return DiffResult(**raw)


__all__ = [
    "SIGNATURE_PRIORITY",
    "CaseDiff",
    "DiffResult",
    "diff_runs",
    "failing_details",
    "failure_signatures",
    "load_diff",
    "save_diff",
]
