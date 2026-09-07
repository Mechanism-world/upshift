"""Spend ceilings for `upshift run` and `upshift upgrade`.

`upshift adapt` makes one paid call and has always had `--max-cost-usd`. `run` and
`upgrade` make thousands — baseline reps, candidate reps, then a repair screen and a
full-suite verify per candidate — and had no ceiling at all, so a single `upgrade`
invocation could quietly blow past a per-case budget that was never enforced anywhere.

The ceiling is priced, not estimated: it sums the token usage that is already recorded on
disk under this run id / tag, through `pricing`, exactly the way `upshift cost` does. So
the number the ceiling enforces and the number `upshift cost` later prints are the same
number.

Two properties matter more than precision:

* **Fail closed.** A model with no published rate is not free. Its usage is charged at the
  most expensive rate in the table and the caller is told at startup, loudly, that it is
  running blind. Silently pricing an unknown model at $0 would turn the ceiling off exactly
  when it is most needed.
* **Stop cleanly.** Crossing the ceiling raises before the next rep is dispatched, so no
  further API call is made. Every finished rep is already written atomically, so rerunning
  the same command resumes from disk and skips them. Nothing is rolled back.

The check happens before each rep is dispatched, which with `--workers N` means up to N-1
reps may already be in flight when the ceiling is crossed. That overshoot is bounded by the
worker count and is the price of concurrency; the ceiling is a brake, not a hard cap on the
last dollar.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from upshift import pricing

#: Written into the tag/run directory when a ceiling stops a pipeline, beside diff.json and
#: verdict.json. Its presence is what says a verdict is NOT to be read as complete.
COST_STOPPED_FILE = "COST_STOPPED.json"


class CostCeilingExceeded(Exception):
    """Raised before dispatching work that would spend past `--max-cost-usd`."""

    def __init__(
        self,
        *,
        phase: str,
        spent_usd: float,
        limit_usd: float,
        unpriced_models: list[str] | None = None,
    ) -> None:
        self.phase = phase
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.unpriced_models = list(unpriced_models or [])
        blind = (
            f" (includes {', '.join(self.unpriced_models)} charged at the highest known rate "
            f"— no published rate for it)"
            if self.unpriced_models
            else ""
        )
        super().__init__(
            f"cost ceiling reached during {phase}: ${spent_usd:.4f} recorded of the "
            f"${limit_usd:.2f} allowed by --max-cost-usd{blind}. Stopped before the next "
            f"API call; every completed rep is on disk, so rerunning the same command with "
            f"a higher --max-cost-usd resumes where this stopped."
        )


def _matching_run_dirs(runs_root: str | Path, prefix: str, descendants: bool) -> list[Path]:
    """Run directories whose records count against this ceiling.

    `upshift run` owns exactly one run id. `upshift upgrade` derives every run id in the
    pipeline from `--tag` — `<tag>-baseline`, `<tag>-candidate`, and one screen/verify/adj
    run per repair candidate — so a tag's spend is the whole `<tag>-*` family.
    """
    root = Path(runs_root)
    if not root.is_dir():
        return []
    found = []
    for directory in sorted(root.iterdir()):
        if not (directory / "manifest.json").is_file():
            continue
        name = directory.name
        if name == prefix or (descendants and name.startswith(f"{prefix}-")):
            found.append(directory)
    return found


def spent_on_disk(
    runs_root: str | Path, prefix: str, *, descendants: bool = False
) -> tuple[float, list[str]]:
    """(USD recorded so far under this run id/tag, model ids that had no published rate)."""
    total = 0.0
    unpriced: set[str] = set()
    for directory in _matching_run_dirs(runs_root, prefix, descendants):
        summary = pricing.run_cost(directory)
        usd, published = pricing.ceiling_price(
            summary["provider"],
            summary["model"],
            summary["input_tokens"],
            summary["output_tokens"],
            summary["cached_input_tokens"],
            summary["cache_creation_input_tokens"],
        )
        total += usd
        if not published:
            unpriced.add(str(summary["model"]))
    return total, sorted(unpriced)


class CostCeiling:
    """Running priced total for one run id or tag, with a limit it refuses to cross.

    `run_suite` calls `check()` before dispatching each rep and `observe()` after each rep is
    recorded; `upshift upgrade` and the repair loop call `check()` again at every phase and
    candidate boundary, so a pipeline stops between phases even when the last phase happened
    to land exactly on the line.
    """

    def __init__(
        self,
        *,
        runs_root: str | Path,
        prefix: str,
        limit_usd: float,
        descendants: bool = False,
    ) -> None:
        if limit_usd <= 0:
            raise ValueError(f"--max-cost-usd must be positive (got {limit_usd})")
        self.runs_root = Path(runs_root)
        self.prefix = prefix
        self.limit_usd = float(limit_usd)
        self.descendants = descendants
        self.phase = "startup"
        self._lock = threading.Lock()
        self._unpriced: set[str] = set()
        self._on_disk = 0.0
        self._since_scan = 0.0
        self.rescan()

    def rescan(self) -> None:
        """Re-price everything on disk. Authoritative: it replaces the in-memory delta."""
        total, unpriced = spent_on_disk(
            self.runs_root, self.prefix, descendants=self.descendants
        )
        with self._lock:
            self._on_disk = total
            self._since_scan = 0.0
            self._unpriced.update(unpriced)

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._on_disk + self._since_scan

    @property
    def unpriced_models(self) -> list[str]:
        with self._lock:
            return sorted(self._unpriced)

    def note_unpriced(self, model: str) -> None:
        with self._lock:
            self._unpriced.add(model)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def observe(self, provider: str, model: str, usage: dict[str, int]) -> None:
        """Charge one just-recorded rep against the ceiling without re-reading the run dir."""
        usd, published = pricing.ceiling_price(
            provider,
            model,
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            int(usage.get("cached_input_tokens", 0)),
            int(usage.get("cache_creation_input_tokens", 0)),
        )
        with self._lock:
            self._since_scan += usd
            if not published:
                self._unpriced.add(model)

    def check(self, phase: str | None = None) -> None:
        """Raise CostCeilingExceeded if the recorded spend has reached the limit."""
        if phase is not None:
            self.phase = phase
        spent = self.spent_usd
        if spent >= self.limit_usd:
            raise CostCeilingExceeded(
                phase=self.phase,
                spent_usd=spent,
                limit_usd=self.limit_usd,
                unpriced_models=self.unpriced_models,
            )


def startup_warning(provider_name: str, models: list[str]) -> str | None:
    """Loud warning when a ceiling is set over a model upshift cannot price, or None.

    The simulator bills nothing, so an unpriced sim model is not blindness — it is $0.
    """
    if provider_name == "sim":
        return None
    blind = sorted({m for m in models if m and not pricing.has_rate(m)})
    if not blind:
        return None
    in_rate, out_rate = pricing.highest_rate()
    return (
        f"--max-cost-usd is set but {', '.join(blind)} has no published rate in "
        f"upshift's pricing table. The ceiling cannot be enforced accurately: usage for "
        f"that model is charged at the most expensive rate upshift knows "
        f"(${in_rate:.2f}/${out_rate:.2f} per 1M tokens) so the run stops early rather "
        f"than late. The reported total is an upper bound, not a bill."
    )


def write_stopped_marker(
    directory: str | Path, exceeded: CostCeilingExceeded, *, extra: dict[str, Any] | None = None
) -> Path:
    """Record the stop beside the pipeline's other artifacts, so no reader mistakes a
    partial pipeline for a finished one."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": "COST_STOPPED",
        "stopped_at": datetime.now(UTC).isoformat(),
        "phase": exceeded.phase,
        "spent_usd": round(exceeded.spent_usd, 6),
        "limit_usd": exceeded.limit_usd,
        "unpriced_models": exceeded.unpriced_models,
        "resume": (
            "rerun the same command with a higher --max-cost-usd; completed reps are "
            "skipped. This marker is deleted when the pipeline finishes."
        ),
    }
    payload.update(extra or {})
    path = directory / COST_STOPPED_FILE
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))
    return path


def clear_stopped_marker(directory: str | Path) -> None:
    """Drop a marker left by an earlier stop once the pipeline has actually completed."""
    (Path(directory) / COST_STOPPED_FILE).unlink(missing_ok=True)
