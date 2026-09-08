"""Run-record persistence. The disk layout here is a contract with differ.py — see DESIGN.md.

runs/<run_id>/
  manifest.json
  cases/<case_id>/rep_<k>.json   (k 1-based, zero-padded to 2)
  summary.json
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from upshift import __version__
from upshift.schemas import AgentConfig, Case, RepRecord, case_set_hash

DEFAULT_RUNS_ROOT = "runs"
THRESHOLDS = {"pass": 0.8, "fail": 0.4}

#: DESIGN.md §A. `scope` is DERIVED by the caller, never declared by the user; the recorder
#: only records it. The default is the historical meaning of every run written before v0.5.
SCOPE_REQUEST_CONTRACT = "request_contract"
SCOPE_ADAPTED_AGENT = "adapted_agent"
SCOPE_NATIVE_APPLICATION = "native_application"
SCOPES = (SCOPE_REQUEST_CONTRACT, SCOPE_ADAPTED_AGENT, SCOPE_NATIVE_APPLICATION)


def evidence_id_for(
    *,
    upshift_version: str,
    provider: str,
    endpoint: str,
    model_requested: str,
    file_hashes: dict[str, str],
    n_reps: int,
    thresholds: dict[str, float],
    scope: str = SCOPE_ADAPTED_AGENT,
    patch_sha256: str | None = None,
) -> str:
    """DESIGN.md §B: the identity of a piece of evidence.

    Two runs share an evidence_id exactly when they asked the same question of the same
    provider with the same agent files, the same repetition count and the same thresholds.
    Anything that could change what the numbers MEAN is an input; nothing that cannot is
    (the run id, the timestamp, the notes and the worker count are all absent on purpose,
    so a rerun under a new tag still resumes and two tags of one experiment still compare).

    ``patch_sha256`` is the sha256 of the patch under trial for a repair run, ``None``
    ("none" in the digest) for an unpatched run: a patched trial is a different experiment
    from the run it was derived from even when every other input is identical.
    """
    payload = json.dumps(
        {
            "upshift_version": str(upshift_version),
            "provider": str(provider),
            "endpoint": str(endpoint),
            "model_requested": str(model_requested),
            "file_hashes": dict(sorted((file_hashes or {}).items())),
            "n_reps": int(n_reps),
            "thresholds": {k: float(v) for k, v in sorted((thresholds or {}).items())},
            "scope": str(scope),
            "patch_sha256": patch_sha256 or "none",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _identity_inputs(manifest: dict[str, Any]) -> dict[str, Any]:
    """The §B inputs of a manifest, flat, for naming what differs when a resume is refused."""
    agent = manifest.get("agent") or {}
    return {
        "upshift_version": manifest.get("upshift_version"),
        "provider": manifest.get("provider"),
        "endpoint": agent.get("endpoint"),
        "model_requested": agent.get("model_requested"),
        "file_hashes": agent.get("file_hashes"),
        "n_reps": manifest.get("n_reps"),
        "thresholds": manifest.get("thresholds"),
        "scope": manifest.get("scope"),
        "patch_sha256": manifest.get("patch_sha256"),
    }


def evidence_id_of(manifest: dict[str, Any]) -> str:
    """The evidence_id a manifest's own recorded inputs imply (recomputed, not read back)."""
    agent = manifest.get("agent") or {}
    return evidence_id_for(
        upshift_version=manifest.get("upshift_version", ""),
        provider=manifest.get("provider", ""),
        endpoint=agent.get("endpoint", ""),
        model_requested=agent.get("model_requested", ""),
        file_hashes=agent.get("file_hashes") or {},
        n_reps=manifest.get("n_reps", 0),
        thresholds=manifest.get("thresholds") or {},
        scope=manifest.get("scope", SCOPE_ADAPTED_AGENT),
        patch_sha256=manifest.get("patch_sha256"),
    )


def recompute_evidence_id(manifest: dict[str, Any]) -> str | None:
    """The evidence_id of the AGENT DIRECTORY AS IT IS ON DISK NOW, under this manifest's
    other inputs — or ``None`` when it cannot be computed.

    Used by `upshift report` to mark a verdict STALE (DESIGN.md §B). It returns None, never
    a guess, when the manifest predates v0.5 (no `agent_dir`), when the directory has been
    moved or deleted, or when a patchable file named by `agent.json` is gone: "we cannot
    check" and "we checked and it changed" are different statements and the report says
    which one it is making.

    Limits, stated because they bound the claim: it re-hashes only the three patchable files
    named by the CURRENT `agent.json`, so a change to `cases/cases.json`, to `backend.py`, or
    to a file the agent.json no longer references is NOT detected here (the case set is
    covered separately by `case_set_hash`); and a run whose agent directory was a temporary
    trial copy (every repair screen/verify run) has an `agent_dir` that no longer exists, so
    it is unfalsifiable by construction and reported as such.
    """
    agent_dir = manifest.get("agent_dir")
    if not agent_dir:
        return None
    directory = Path(agent_dir)
    agent_json = directory / "agent.json"
    if not agent_json.is_file():
        return None
    try:
        raw = json.loads(agent_json.read_text())
        hashes = {}
        for rel in ("agent.json", raw["system_prompt_file"], raw["tools_file"]):
            hashes[rel] = hashlib.sha256((directory / rel).read_bytes()).hexdigest()
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    fresh = dict(manifest)
    fresh["agent"] = dict(manifest.get("agent") or {}, file_hashes=hashes)
    return evidence_id_of(fresh)


#: A run id and a case id both become one directory name under the runs root. Neither may
#: contain a path separator or be a `..` hop: `--tag` is a CLI argument and a case id comes
#: out of a `cases.json` that `upshift adapt` may have drafted from a repository nobody
#: audited, and the repair loop `rmtree`s the run directory it is handed. Everything upshift
#: writes stays under the runs root, and this is what makes that true.
_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def safe_component(value: str, what: str) -> str:
    """`value` if it is usable as a single directory name, else ValueError."""
    text = str(value)
    if not _SAFE_COMPONENT_RE.fullmatch(text) or ".." in text:
        raise ValueError(
            f"invalid {what} {value!r}: use letters, digits, '.', '_' and '-' only "
            f"(it names a directory under the runs root)"
        )
    return text


def run_dir(runs_root: str | Path, run_id: str) -> Path:
    return Path(runs_root) / safe_component(run_id, "run id")


def rep_path(run_directory: Path, case_id: str, rep: int) -> Path:
    return run_directory / "cases" / safe_component(case_id, "case id") / f"rep_{rep:02d}.json"


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
    scope: str = SCOPE_ADAPTED_AGENT,
    patch_sha256: str | None = None,
) -> dict[str, Any]:
    """``scope`` is DESIGN.md §A's verification scope, derived by the caller (the runner knows
    whether it drove a native application, a replay stub or an adapted backend). The default
    is what every pre-v0.5 run meant, so an existing caller that does not pass it keeps its
    recorded meaning instead of silently acquiring a stronger one."""
    if scope not in SCOPES:
        raise ValueError(f"unknown verification scope {scope!r}; expected one of {SCOPES}")
    file_hashes = config.file_hashes()
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "agent": {
            "name": config.name,
            "endpoint": endpoint,
            "model_requested": model_requested,
            "params": params,
            "max_turns": config.max_turns,
            "file_hashes": file_hashes,
        },
        # The directory the config was loaded from, so `upshift report` can re-hash it later
        # and say whether the evidence still describes what is on disk (DESIGN.md §B).
        "agent_dir": str(Path(config.agent_dir).resolve()) if config.agent_dir else "",
        "scope": scope,
        "patch_sha256": patch_sha256,
        "n_reps": n_reps,
        "thresholds": dict(THRESHOLDS),
        "case_set_hash": case_set_hash(cases),
        "upshift_version": __version__,
        "notes": notes,
    }
    manifest["evidence_id"] = evidence_id_for(
        upshift_version=__version__,
        provider=provider,
        endpoint=endpoint,
        model_requested=model_requested,
        file_hashes=file_hashes,
        n_reps=n_reps,
        thresholds=THRESHOLDS,
        scope=scope,
        patch_sha256=patch_sha256,
    )
    run_directory.mkdir(parents=True, exist_ok=True)
    existing = run_directory / "manifest.json"
    if existing.exists():
        prev = json.loads(existing.read_text())
        # Resuming: the run being resumed must be the same experiment (DESIGN.md §B).
        # The evidence_id is checked FIRST because it is the whole identity in one value;
        # the per-key guards below stay as the fallback for manifests written before v0.5,
        # which carry no evidence_id and must remain readable and resumable.
        prev_id = prev.get("evidence_id")
        if prev_id and prev_id != manifest["evidence_id"]:
            before, now = _identity_inputs(prev), _identity_inputs(manifest)
            differing = [k for k in now if before.get(k) != now.get(k)]
            detail = "; ".join(
                f"{k}: {before.get(k)!r} -> {now.get(k)!r}" for k in differing
            ) or "the recorded evidence_id does not match its own inputs"
            raise ValueError(
                f"run {run_id!r} was recorded under a different evidence_id "
                f"({prev_id[:12]} != {manifest['evidence_id'][:12]}): refusing to resume it, "
                f"because the reps already on disk answered a different question. "
                f"Differing inputs: {detail}. Use a new --tag, or restore the inputs."
            )
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
