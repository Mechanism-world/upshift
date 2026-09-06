"""Final verdict: SAFE / SAFE WITH PATCH / STAY PINNED.

The verdict is deliberately conservative:
- SAFE requires zero regressed cases. Flaky degradations do not block, but are listed.
- SAFE WITH PATCH requires every regressed case restored and zero previously-passing cases
  broken, proven by a full-suite verification run of the patched agent.
- Anything less is STAY PINNED.
"""

from __future__ import annotations

from typing import Any

from upshift.differ import DiffResult
from upshift.repair.loop import RepairOutcome
from upshift.schemas import LABEL_FLAKY, LABEL_IMPROVED, LABEL_REGRESSED

SAFE = "SAFE"
SAFE_WITH_PATCH = "SAFE WITH PATCH"
STAY_PINNED = "STAY PINNED"


def decide(
    diff: DiffResult,
    repair_outcome: RepairOutcome | None = None,
    patch_path: str | None = None,
    framework: str | None = None,
) -> dict[str, Any]:
    """``framework`` is the framework a capture-derived agent directory was built from
    (``upshift.capture.mapping.framework_of``). It is carried in the verdict so a report
    rendered later, from verdict.json alone, still knows where each repair lives."""
    regressed = sorted(c.case_id for c in diff.cases if c.label == LABEL_REGRESSED)
    flaky = sorted(c.case_id for c in diff.cases if c.label == LABEL_FLAKY)
    improved = sorted(c.case_id for c in diff.cases if c.label == LABEL_IMPROVED)

    if not regressed:
        verdict = SAFE
        restored: list[str] = []
        unrestored: list[str] = []
        repair_log: list[str] = []
    elif repair_outcome is not None and not repair_outcome.unrestored:
        verdict = SAFE_WITH_PATCH
        restored = repair_outcome.restored
        unrestored = []
        repair_log = repair_outcome.log
    else:
        verdict = STAY_PINNED
        restored = repair_outcome.restored if repair_outcome else []
        unrestored = repair_outcome.unrestored if repair_outcome else regressed
        repair_log = repair_outcome.log if repair_outcome else []

    return {
        "verdict": verdict,
        "provider": diff.candidate_manifest.get("provider"),
        "baseline_model": diff.baseline_manifest["agent"]["model_requested"],
        "candidate_model": diff.candidate_manifest["agent"]["model_requested"],
        "regressed_total": len(regressed),
        "regressed": regressed,
        "restored": len(restored),
        "restored_cases": restored,
        "unrestored": unrestored,
        # The repair loop never keeps a candidate that breaks a passing case, so an
        # accepted patch always has zero collateral damage by construction.
        "broken_by_patch": 0,
        "flaky": flaky,
        "improved": improved,
        "patch_path": patch_path if verdict == SAFE_WITH_PATCH else None,
        "repair_log": repair_log,
        "framework": framework,
        # The accepted repairs, structured. `repair_log` is prose for a human to read; this is
        # what the report's framework mapping is keyed on (upshift.capture.mapping).
        "accepted_patches": [
            {"id": p.id, "repair_type": p.repair_type, "description": p.description}
            for p in (repair_outcome.accepted_patches if repair_outcome else [])
        ],
    }
