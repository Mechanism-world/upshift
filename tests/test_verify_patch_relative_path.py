"""Regression (found on a fresh-install check, 2026-09-07): `verify-patch` ran `git apply`
inside the clean temp copy, so a RELATIVE --patch path — what every user types — could not be
opened ("can't open patch 'runs/demo/upgrade.patch'"). Paths are resolved before the cwd
changes.

Two proofs at two altitudes: `apply_patch` itself with relative arguments, and the whole
`upshift verify-patch` command run from a working directory with exactly the relative paths
the README prints.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift import cli, recorder
from upshift.patch import make_patch

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="verify-patch shells out to git apply"
)


def _example_agent(path: Path) -> Path:
    for rel in cli.example_agent_files():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cli.read_example_agent_file(rel))
    return path


def test_apply_patch_accepts_relative_agent_and_patch_paths(tmp_path, monkeypatch) -> None:
    from upshift import verify_patch as vp

    agent = _example_agent(tmp_path / "agent")
    patched = tmp_path / "patched"
    shutil.copytree(agent, patched)
    cfg = patched / "agent.json"
    cfg.write_text(cfg.read_text().replace('"chat_completions"', '"responses"'))
    (tmp_path / "runs" / "demo").mkdir(parents=True)
    patch_file = tmp_path / "runs" / "demo" / "upgrade.patch"
    patch_file.write_text(make_patch(agent, patched, rel_prefix="agent"))

    monkeypatch.chdir(tmp_path)
    # Relative paths, exactly as typed on the command line. `apply_patch` raises
    # VerificationError when the patch cannot be read or applied, so returning at all is
    # the first half of the proof.
    result = vp.apply_patch(Path("agent"), Path("runs/demo/upgrade.patch"), tmp_path / "clean")

    assert set(result) == {"commands", "strip_level"}
    assert any(command.startswith("git apply") for command in result["commands"])
    # The clean copy really carries the patched content, not just an exit code.
    assert '"responses"' in (tmp_path / "clean" / "agent.json").read_text()
    assert '"chat_completions"' in (agent / "agent.json").read_text(), "original untouched"
    # git ran with cwd=dest via subprocess; the process's own cwd must be where we left it.
    assert Path(os.getcwd()).resolve() == tmp_path.resolve()


def test_the_command_accepts_the_relative_paths_the_readme_prints(tmp_path, monkeypatch) -> None:
    _example_agent(tmp_path / "agent")
    monkeypatch.chdir(tmp_path)

    upgrade = cli.main(
        [
            "upgrade", "--agent", "agent", "--provider", "sim",
            "--baseline-model", "sim-5.5", "--candidate-model", "sim-5.6-sol",
            "--tag", "demo", "--quiet",
        ]
    )
    assert upgrade == 0
    out = recorder.run_dir(Path("runs"), "demo")
    assert json.loads((out / "verdict.json").read_text())["verdict"] == "SAFE WITH PATCH"

    code = cli.main(
        [
            "verify-patch",
            "--agent", "agent",
            "--patch", "runs/demo/upgrade.patch",
            "--run", "runs/demo-final",
        ]
    )

    assert code == 0
    assert Path(os.getcwd()).resolve() == tmp_path.resolve()
