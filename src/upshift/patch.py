"""Emit accepted repairs as a git-applyable unified diff over the victim's files.

The exported patch must be EXACTLY what the repair loop verified. `patched_agent/` is the
directory that was run; `upgrade.patch` is what the user applies. Anything the diff cannot
express is a silent divergence between the two, so this module refuses to export instead of
narrowing the claim (see `_divergences`).
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

PATCHABLE = ("agent.json",)  # plus the prompt/tools files named inside agent.json

#: Build/venv droppings a run leaves in a working copy. They are not agent files and their
#: presence in one directory and not the other says nothing about the repair.
IGNORED = ("__pycache__", ".pytest_cache", ".DS_Store")


def patchable_files(agent_dir: Path) -> list[str]:
    raw = json.loads((agent_dir / "agent.json").read_text())
    return ["agent.json", raw["system_prompt_file"], raw["tools_file"]]


class PatchExportError(ValueError):
    """The patched directory differs from the original in a way the patch cannot carry."""


def _relevant_files(root: Path) -> dict[str, Path]:
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in IGNORED or part.endswith(".pyc") for part in rel.parts):
            continue
        out[str(rel)] = path
    return out


def _divergences(original_dir: Path, patched_dir: Path, patchable: set[str]) -> list[str]:
    """Everything that differs between the two directories and is NOT in the exported diff.

    Two classes, both of which used to pass silently:

    - a file outside the three patchable ones that was added, removed or changed — the loop
      verified it and `git apply` would never reproduce it;
    - a mode change on a patchable file — `difflib` produces content hunks only, so an
      executable bit set in `patched_agent/` never reaches the user.
    """
    before, after = _relevant_files(original_dir), _relevant_files(patched_dir)
    problems = []
    for rel in sorted(set(before) | set(after)):
        if rel in patchable:
            if rel in before and rel in after:
                old_mode = before[rel].stat().st_mode & 0o777
                new_mode = after[rel].stat().st_mode & 0o777
                if old_mode != new_mode:
                    problems.append(f"{rel}: file mode {old_mode:o} -> {new_mode:o}")
            continue
        if rel not in after:
            problems.append(f"{rel}: deleted in the patched agent")
        elif rel not in before:
            problems.append(f"{rel}: added by the patched agent")
        elif before[rel].read_bytes() != after[rel].read_bytes():
            problems.append(f"{rel}: changed in the patched agent")
    return problems


def _unified(old: str, new: str, repo_rel: str) -> str:
    """Unified diff of two texts, with the `\\ No newline at end of file` markers git needs.

    ``difflib`` emits a hunk line with no trailing newline for a file that ends without one
    and says nothing about it, which makes the patch either unappliable or silently
    newline-adding. git's own marker is what makes the exported patch reproduce the file the
    loop actually ran.
    """
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{repo_rel}",
            tofile=f"b/{repo_rel}",
        )
    )
    out = []
    for line in lines:
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)


def make_patch(
    original_dir: str | Path,
    patched_dir: str | Path,
    rel_prefix: str,
    header: str = "",
) -> str:
    """Unified diff of the patchable files, with paths rooted at the repo (rel_prefix, e.g.
    'victim/booking_agent') so the output applies with `git apply`.

    ``header`` is prose written above the first `diff --git` line — where these same repairs
    live in the framework the agent was captured from (upshift.capture.mapping.patch_header).
    Git skips everything before that line, so the patch still applies unchanged; a reader who
    opens the file sees where the change really belongs before they see the diff.

    Raises ``PatchExportError`` when the patched directory differs from the original in a way
    a unified diff over the patchable files cannot carry, rather than exporting a patch that
    is not what was verified.
    """
    original_dir, patched_dir = Path(original_dir), Path(patched_dir)
    files = sorted(set(patchable_files(original_dir)) | set(patchable_files(patched_dir)))
    problems = _divergences(original_dir, patched_dir, set(files))
    if problems:
        raise PatchExportError(
            "the patched agent differs from the original in ways this patch cannot carry, so "
            "the exported patch would not be what the repair loop verified: "
            + "; ".join(problems)
            + ". Repairs are limited to agent.json, the system prompt and the tools file "
            "(DESIGN.md, 'Repair loop')."
        )
    chunks: list[str] = []
    for rel in files:
        old = (original_dir / rel).read_text() if (original_dir / rel).exists() else ""
        new = (patched_dir / rel).read_text() if (patched_dir / rel).exists() else ""
        if old == new:
            continue
        repo_rel = f"{rel_prefix}/{rel}"
        chunks.append(f"diff --git a/{repo_rel} b/{repo_rel}\n" + _unified(old, new, repo_rel))
    if not chunks:
        return ""
    return header + "".join(chunks)
