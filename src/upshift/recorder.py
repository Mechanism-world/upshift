"""Run-record persistence. The disk layout here is a contract with differ.py — see DESIGN.md.

runs/<run_id>/
  manifest.json
  cases/<case_id>/rep_<k>.json   (k 1-based, zero-padded to 2)
  summary.json
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upshift import __version__
from upshift.schemas import AgentConfig, Case, RepRecord, case_set_hash

DEFAULT_RUNS_ROOT = "runs"
THRESHOLDS = {"pass": 0.8, "fail": 0.4}


def run_dir(runs_root: str | Path, run_id: str) -> Path:
    return Path(runs_root) / run_id


def rep_path(run_directory: Path, case_id: str, rep: int) -> Path:
    return run_directory / "cases" / case_id / f"rep_{rep:02d}.json"


def write_manifest(
    run_directory: Path,
    *,
    run_id: str,
    provider: str,
    config: AgentConfig,
    model_requested: str,
    endpoint: str,
    params: dict[str, Any],
    n_reps: int,
    cases: list[Case],
    notes: str = "",
) -> dict[str, Any]:
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "agent": {
            "name": config.name,
            "endpoint": endpoint,
            "model_requested": model_requested,
            "params": params,
            "max_turns": config.max_turns,
            "file_hashes": config.file_hashes(),
        },
        "n_reps": n_reps,
        "thresholds": dict(THRESHOLDS),
        "case_set_hash": case_set_hash(cases),
        "upshift_version": __version__,
        "notes": notes,
    }
    run_directory.mkdir(parents=True, exist_ok=True)
    existing = run_directory / "manifest.json"
    if existing.exists():
        prev = json.loads(existing.read_text())
        # Resuming: the run being resumed must be the same experiment.
        for key in ("provider", "n_reps", "case_set_hash"):
            if prev.get(key) != manifest[key]:
                raise ValueError(
                    f"run {run_id!r} exists with different {key}; refusing to mix experiments"
                )
        if prev.get("agent") != manifest["agent"]:
            raise ValueError(
                f"run {run_id!r} exists with a different agent config; refusing to mix experiments"
            )
        return prev
    _atomic_write(existing, json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def is_rep_complete(run_directory: Path, case_id: str, rep: int) -> bool:
    path = rep_path(run_directory, case_id, rep)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return "passed" in data
    except (json.JSONDecodeError, OSError):
        return False


def write_rep(run_directory: Path, record: RepRecord) -> None:
    path = rep_path(run_directory, record.case_id, record.rep)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(dataclasses.asdict(record), indent=1, sort_keys=True))


def load_rep(run_directory: Path, case_id: str, rep: int) -> RepRecord:
    return RepRecord.from_dict(json.loads(rep_path(run_directory, case_id, rep).read_text()))


def load_case_reps(run_directory: Path, case_id: str) -> list[RepRecord]:
    case_dir = run_directory / "cases" / case_id
    records = []
    for path in sorted(case_dir.glob("rep_*.json")):
        records.append(RepRecord.from_dict(json.loads(path.read_text())))
    return records


def write_summary(run_directory: Path) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    cases_dir = run_directory / "cases"
    for case_dir in sorted(cases_dir.iterdir()):
        if not case_dir.is_dir():
            continue
        reps = load_case_reps(run_directory, case_dir.name)
        summary[case_dir.name] = {
            "passes": sum(1 for r in reps if r.passed),
            "n": len(reps),
        }
    _atomic_write(run_directory / "summary.json", json.dumps(summary, indent=1, sort_keys=True))
    return summary


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)
