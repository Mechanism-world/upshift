"""The shipped example agents under `examples/runners/` actually run.

`examples/runners/README.md` tells a developer to copy a reference runner and write an
`agent.json` with a `runner` block. Until v0.5 nothing in the repository showed the finished
result, so the three-step integration was documented but never demonstrated, and nothing
stopped it from rotting. These tests drive both shipped agents through the real CLI —
`upshift run --allow-runner` — and assert what a user would see: every rep passes and every
record is stamped `scope: native_application`.

No key, no network, no cost: the reference runners drive the stub provider in
`fake_provider.py` (Python) / an inline class (Node).

A runner's `workdir` may never escape its agent directory (native/protocol.py,
`resolved_workdir`), and `workdir-copy` copies only that directory — so a shipped agent cannot
point at `../python_runner.py`. Each agent therefore carries the reference runner inside its
own `app/` workdir, and `test_the_shipped_runners_are_the_reference_runners_verbatim` pins
those files to their siblings byte for byte, so the two can never drift apart silently.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift import cli, recorder
from upshift.schemas import SCOPE_NATIVE_APPLICATION

pytestmark = [pytest.mark.sim]

EXAMPLES = ROOT / "examples" / "runners"
PYTHON_AGENT = EXAMPLES / "python-agent"
NODE_AGENT = EXAMPLES / "node-agent"

#: Every case id in the shipped suite. Both agents ship the same suite on purpose: the
#: protocol, not the language, is what a reader is being shown.
CASE_IDS = ("add_one_task", "append_to_existing_list", "answers_without_extra_tools")

N_REPS = 2


def _run(agent: Path, tmp_path: Path, run_id: str) -> Path:
    """Exactly the command examples/runners/README.md tells a user to type."""
    runs_root = tmp_path / "runs"
    exit_code = cli.main(
        [
            "run",
            "--agent", str(agent),
            "--provider", "sim",  # never called: a native run builds its own requests
            "--run-id", run_id,
            "--n", str(N_REPS),
            "--runs-root", str(runs_root),
            "--workers", "2",
            "--quiet",
            "--allow-runner",
        ]
    )
    assert exit_code == 0, f"`upshift run` on {agent.name} exited {exit_code}"
    return runs_root / run_id


def _assert_all_reps_passed_natively(run_directory: Path) -> None:
    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary == {case_id: {"passes": N_REPS, "n": N_REPS} for case_id in CASE_IDS}
    for case_id in CASE_IDS:
        for rep in range(1, N_REPS + 1):
            record = recorder.load_rep(run_directory, case_id, rep)
            assert record.passed, f"{case_id} rep {rep}: {record.check_results}"
            assert record.api_error is None
            assert record.scope == SCOPE_NATIVE_APPLICATION
            assert record.episode_source == "native_application"
            assert record.runner["exit_code"] == 0
            assert record.runner["isolation"] == "workdir-copy"


def test_the_shipped_python_agent_runs_through_the_cli_and_every_rep_passes(tmp_path) -> None:
    _assert_all_reps_passed_natively(_run(PYTHON_AGENT, tmp_path, "examples-python"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_shipped_node_agent_runs_through_the_cli_and_every_rep_passes(tmp_path) -> None:
    _assert_all_reps_passed_natively(_run(NODE_AGENT, tmp_path, "examples-node"))


def test_the_shipped_runners_are_the_reference_runners_verbatim() -> None:
    """A shipped agent must demonstrate the file the README tells you to copy, not a fork."""
    for agent, names in (
        (PYTHON_AGENT, ("python_runner.py", "fake_provider.py")),
        (NODE_AGENT, ("node_runner.mjs",)),
    ):
        for name in names:
            shipped = (agent / "app" / name).read_bytes()
            reference = (EXAMPLES / name).read_bytes()
            assert shipped == reference, (
                f"{agent.name}/app/{name} has drifted from examples/runners/{name}; "
                f"copy the reference file over it."
            )


def test_both_agents_declare_the_runner_block_the_readme_documents() -> None:
    for agent, command in (
        (PYTHON_AGENT, ["python3", "python_runner.py"]),
        (NODE_AGENT, ["node", "node_runner.mjs"]),
    ):
        config = json.loads((agent / "agent.json").read_text())
        runner = config["runner"]
        assert config["model"] == "stub-model-a"  # the stub that answers; -b rejects tools
        assert runner["command"] == command
        assert runner["workdir"] == "app"
        assert runner["isolation"] == "workdir-copy"
        assert runner["timeout_s"] > 0
        # The whole authoring surface: no system_prompt.txt, no tools.json, no backend.py.
        for absent in ("system_prompt.txt", "tools.json", "backend.py"):
            assert not (agent / absent).exists()
