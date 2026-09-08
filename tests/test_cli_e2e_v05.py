"""The v0.5 surfaces, driven through `cli.main` exactly as a user drives them.

Every other v0.5 test calls a function; these call the COMMAND, because the integration risk
of this release is not in the pieces — it is in whether the flags reach them. Four proofs, all
on the simulator or a stub-provider runner, so the whole file costs $0 and needs no key:

a. the packaged example agent still ends `SAFE WITH PATCH` through `upshift upgrade`, with the
   v0.5 report sections (scope, collateral sentence, evidence ids, fresh final run);
b. a native-runner agent runs through `upshift run --allow-runner` and records
   `scope: native_application` — and refuses, printing its argv, without the flag;
c. `upshift verify-patch` on the patch that upgrade exported exits 0;
d. an agent whose suite is empty exits 3 with INCONCLUSIVE(empty_suite), not 2 and never SAFE.
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
from upshift.verdict import EXIT_INCONCLUSIVE, INCONCLUSIVE, SAFE_WITH_PATCH, STAY_PINNED

pytestmark = [pytest.mark.sim]

EXAMPLES = ROOT / "examples" / "runners"

NATIVE_CASES = [
    {
        "id": "add_one_task",
        "description": "The application's own loop adds the task it was asked for.",
        "initial_state": {"tasks": []},
        "user_messages": ["Add a task to call the dentist."],
        "checks": [
            {"type": "no_api_error"},
            {"type": "tool_called", "name": "add_task"},
            {"type": "state_count", "path": "tasks", "equals": 1},
        ],
    }
]


def _example_agent(path: Path) -> Path:
    """The packaged example agent, written out the way `upshift init` writes it."""
    for rel in cli.example_agent_files():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cli.read_example_agent_file(rel))
    return path


def _native_agent(path: Path) -> Path:
    """An agent whose whole authoring surface is a `runner` block and `cases/`."""
    (path / "app").mkdir(parents=True)
    (path / "cases").mkdir(parents=True)
    for name in ("python_runner.py", "fake_provider.py"):
        shutil.copy(EXAMPLES / name, path / "app" / name)
    (path / "agent.json").write_text(
        json.dumps(
            {
                "name": "native-task-agent",
                "model": "stub-model-a",
                "endpoint": "chat_completions",
                "runner": {
                    "kind": "command",
                    "workdir": "app",
                    "command": [sys.executable, "python_runner.py"],
                    "env": {"DEMO_MODEL": "{model}"},
                    "timeout_s": 60,
                },
            },
            indent=2,
        )
    )
    (path / "cases" / "cases.json").write_text(json.dumps(NATIVE_CASES))
    return path


@pytest.fixture(scope="module")
def sim_upgrade(tmp_path_factory):
    """One `upshift upgrade` on the simulator, shared by proofs (a) and (c)."""
    work = tmp_path_factory.mktemp("e2e")
    agent = _example_agent(work / "agent")
    runs = work / "runs"
    code = cli.main(
        [
            "upgrade", "--agent", str(agent), "--provider", "sim",
            "--baseline-model", "sim-5.5", "--candidate-model", "sim-5.6-sol",
            "--tag", "e2e", "--runs-root", str(runs), "--quiet",
        ]
    )
    out = recorder.run_dir(runs, "e2e")
    return {
        "exit_code": code,
        "agent": agent,
        "runs": runs,
        "out": out,
        "verdict": json.loads((out / "verdict.json").read_text()),
        "report": (out / "REPORT.md").read_text(),
    }


# ---------------------------------------------------------------------------
# a. the whole pipeline, through the CLI
# ---------------------------------------------------------------------------


def test_the_example_agent_still_ends_safe_with_patch_through_the_cli(sim_upgrade):
    verdict = sim_upgrade["verdict"]

    assert sim_upgrade["exit_code"] == 0
    assert verdict["verdict"] == SAFE_WITH_PATCH
    assert verdict["broken_by_patch"] == 0
    assert verdict["restored"] == verdict["regressed_total"] > 0
    assert (sim_upgrade["out"] / "upgrade.patch").is_file()


def test_the_report_carries_every_v05_section(sim_upgrade):
    report, verdict = sim_upgrade["report"], sim_upgrade["verdict"]

    # §A scope, and the sentence that defines it — never "verified in the application".
    assert "Verification scope: adapted_agent" in report
    assert verdict["scope"] == "adapted_agent"
    assert "NOT verified in the application" in report
    # §D collateral protection is reported, not assumed…
    assert "collateral protection was not exercised on this run" in report
    assert verdict["collateral"]["exercised"] is False
    # …the verdict rests on a FRESH final run…
    assert verdict["final_run_id"] == "e2e-final"
    assert verdict["evidence_label"] == "fresh_final_verification"
    assert "rests on the FRESH final verification run" in report
    assert recorder.run_dir(sim_upgrade["runs"], "e2e-final").is_dir()
    # …and §B evidence identity is printed for the runs it rests on.
    assert "evidence ids:" in report
    assert "e2e-baseline=" in report and "e2e-candidate=" in report


# ---------------------------------------------------------------------------
# b. the native runner, through the CLI
# ---------------------------------------------------------------------------


def test_a_native_agent_runs_through_the_cli_and_records_native_scope(tmp_path):
    agent = _native_agent(tmp_path / "agent")
    runs = tmp_path / "runs"

    code = cli.main(
        [
            "run", "--agent", str(agent), "--provider", "sim", "--run-id", "native",
            "--runs-root", str(runs), "--n", "2", "--quiet", "--allow-runner",
        ]
    )

    assert code == 0
    run_directory = recorder.run_dir(runs, "native")
    manifest = json.loads((run_directory / "manifest.json").read_text())
    assert manifest["scope"] == "native_application"
    for rep in (1, 2):
        record = recorder.load_rep(run_directory, "add_one_task", rep)
        assert record.scope == "native_application"
        assert record.episode_source == "native_application"
        assert record.passed
        assert record.runner["exit_code"] == 0


def test_without_allow_runner_the_cli_prints_the_argv_and_runs_nothing(tmp_path, capsys):
    agent = _native_agent(tmp_path / "agent")
    runs = tmp_path / "runs"

    code = cli.main(
        [
            "run", "--agent", str(agent), "--provider", "sim", "--run-id", "native",
            "--runs-root", str(runs), "--n", "2", "--quiet",
        ]
    )

    assert code == 2
    printed = capsys.readouterr().out
    assert "python_runner.py" in printed
    assert "--allow-runner" in printed
    assert not recorder.run_dir(runs, "native").exists(), "nothing may be executed or recorded"


# ---------------------------------------------------------------------------
# c. verify-patch on what upgrade exported
# ---------------------------------------------------------------------------


def test_verify_patch_on_the_exported_patch_exits_zero(sim_upgrade):
    code = cli.main(
        [
            "verify-patch",
            "--agent", str(sim_upgrade["agent"]),
            "--patch", str(sim_upgrade["out"] / "upgrade.patch"),
            "--run", str(recorder.run_dir(sim_upgrade["runs"], "e2e-final")),
        ]
    )

    assert code == 0


# ---------------------------------------------------------------------------
# d. an empty suite is INCONCLUSIVE, not a usage error and never SAFE
# ---------------------------------------------------------------------------


def test_an_empty_suite_exits_inconclusive_with_a_verdict_on_disk(tmp_path):
    agent = _example_agent(tmp_path / "agent")
    (agent / "cases" / "cases.json").write_text("[]")
    runs = tmp_path / "runs"

    code = cli.main(
        [
            "upgrade", "--agent", str(agent), "--provider", "sim",
            "--baseline-model", "sim-5.5", "--candidate-model", "sim-5.6-sol",
            "--tag", "empty", "--runs-root", str(runs), "--quiet",
        ]
    )

    assert code == EXIT_INCONCLUSIVE
    verdict = json.loads((recorder.run_dir(runs, "empty") / "verdict.json").read_text())
    assert verdict["verdict"] == INCONCLUSIVE
    assert verdict["inconclusive_reason"] == "empty_suite"
    assert verdict["reasons"] == ["empty_suite"]
    # Nothing was run: no baseline directory, so no money and no time was spent finding out.
    assert not recorder.run_dir(runs, "empty-baseline").exists()


# ---------------------------------------------------------------------------
# e. a native agent reaches a verdict without --no-repair (repairs are skipped, not crashed)
# ---------------------------------------------------------------------------


def test_a_regressed_native_agent_reaches_a_verdict_without_no_repair(tmp_path, capsys):
    """The repair playbook edits adapter files a native agent does not have, so `upgrade`
    used to die with a KeyError in `generate_candidates` AFTER both runs were paid for.
    DESIGN.md §C says repairs are not generated in native mode; this proves the pipeline
    enforces it and still produces a verdict."""
    agent = _native_agent(tmp_path / "agent")
    runs = tmp_path / "runs"

    code = cli.main(
        [
            "upgrade", "--agent", str(agent), "--provider", "sim",
            "--baseline-model", "stub-model-a", "--candidate-model", "stub-model-b",
            "--tag", "native-upgrade", "--runs-root", str(runs), "--n", "2",
            "--allow-runner", "--quiet",
        ]
    )

    printed = capsys.readouterr().out
    out = recorder.run_dir(runs, "native-upgrade")
    verdict = json.loads((out / "verdict.json").read_text())

    assert code == 1
    assert verdict["verdict"] == STAY_PINNED
    assert verdict["regressed"] == ["add_one_task"]
    # Skipped, not attempted: no repair candidates, and no patched_agent/ directory.
    assert verdict["repair_log"] == []
    assert verdict["patch_path"] is None
    assert not (out / "patched_agent").exists()
    assert "repairs are not generated for native" in printed
