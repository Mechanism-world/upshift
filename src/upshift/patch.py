"""Emit accepted repairs as a git-applyable unified diff over the victim's files."""

from __future__ import annotations

import difflib
import json
from pathlib import Path

PATCHABLE = ("agent.json",)  # plus the prompt/tools files named inside agent.json


def patchable_files(agent_dir: Path) -> list[str]:
    raw = json.loads((agent_dir / "agent.json").read_text())
    return ["agent.json", raw["system_prompt_file"], raw["tools_file"]]


def make_patch(original_dir: str | Path, patched_dir: str | Path, rel_prefix: str) -> str:
    """Unified diff of the patchable files, with paths rooted at the repo (rel_prefix, e.g.
    'victim/booking_agent') so the output applies with `git apply`."""
    original_dir, patched_dir = Path(original_dir), Path(patched_dir)
    files = sorted(set(patchable_files(original_dir)) | set(patchable_files(patched_dir)))
    chunks: list[str] = []
    for rel in files:
        old = (original_dir / rel).read_text() if (original_dir / rel).exists() else ""
        new = (patched_dir / rel).read_text() if (patched_dir / rel).exists() else ""
        if old == new:
            continue
        repo_rel = f"{rel_prefix}/{rel}"
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{repo_rel}",
            tofile=f"b/{repo_rel}",
        )
        chunks.append(f"diff --git a/{repo_rel} b/{repo_rel}\n" + "".join(diff))
    return "".join(chunks)
