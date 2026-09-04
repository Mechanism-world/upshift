"""`upshift init` and the CLI's front door: scaffolding, agent-dir resolution, error messages.

These are the paths a stranger hits in their first five minutes, so every failure asserted
here must be a one-line message with a next step in it — never a traceback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift import cli
from upshift.providers.base import ProviderAPIError
from upshift.schemas import AgentConfig, Case

VICTIM_DIR = ROOT / "victim" / "booking_agent"
EXPECTED_FILES = [
    "agent.json",
    "backend.py",
    "cases/cases.json",
    "system_prompt.txt",
    "tools.json",
]


# ---------------------------------------------------------------------------
# The packaged example agent
# ---------------------------------------------------------------------------


def test_packaged_example_ships_every_file_an_agent_dir_needs():
    assert cli.example_agent_files() == EXPECTED_FILES


def test_packaged_example_matches_the_committed_victim_agent():
    """The package copy is the template for the committed experiment agent; keep them equal."""
    for rel in EXPECTED_FILES:
        assert cli.read_example_agent_file(rel) == (VICTIM_DIR / rel).read_bytes(), rel


# ---------------------------------------------------------------------------
# upshift init
# ---------------------------------------------------------------------------


def test_init_writes_byte_identical_copies_of_the_packaged_example(tmp_path):
    dest = tmp_path / "my-agent"
    assert cli.main(["init", str(dest)]) == 0
    written = sorted(
        str(p.relative_to(dest).as_posix()) for p in dest.rglob("*") if p.is_file()
    )
    assert written == EXPECTED_FILES
    for rel in EXPECTED_FILES:
        assert (dest / rel).read_bytes() == cli.read_example_agent_file(rel), rel


def test_init_scaffolds_a_directory_upshift_can_actually_run(tmp_path):
    dest = tmp_path / "my-agent"
    cli.main(["init", str(dest)])
    raw = cli.validate_agent_dir(dest)
    assert raw["name"] == "booking-agent"
    config = AgentConfig.load(dest)
    assert config.tools and config.system_prompt.strip()
    assert len(Case.load_all(dest / "cases" / "cases.json")) == len(
        json.loads(cli.read_example_agent_file("cases/cases.json"))
    )


def test_init_accepts_an_existing_empty_directory(tmp_path):
    dest = tmp_path / "empty"
    dest.mkdir()
    assert cli.main(["init", str(dest)]) == 0
    assert (dest / "agent.json").is_file()


def test_init_refuses_a_non_empty_directory_and_writes_nothing(tmp_path, capsys):
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine")
    assert cli.main(["init", str(dest)]) == 2
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]
    out = flat(capsys)
    assert "not empty" in out and "nothing was written" in out


def test_init_refuses_a_path_that_is_a_file(tmp_path, capsys):
    dest = tmp_path / "afile"
    dest.write_text("x")
    assert cli.main(["init", str(dest)]) == 2
    assert "not a directory" in flat(capsys)


def test_init_prints_the_two_commands_to_run_next(tmp_path, capsys):
    dest = tmp_path / "my-agent"
    cli.main(["init", str(dest)])
    out = flat(capsys)
    assert "--provider sim" in out
    assert cli.SIM_BASELINE_MODEL in out and cli.SIM_CANDIDATE_MODEL in out
    assert "OPENAI_API_KEY" in out
    assert "runs" in out  # where the artifacts land


# ---------------------------------------------------------------------------
# Agent directory resolution + validation
# ---------------------------------------------------------------------------


@pytest.fixture
def agent(tmp_path):
    dest = tmp_path / "my-agent"
    cli.main(["init", str(dest)])
    return dest


def flat(capsys) -> str:
    """Console output as one line: rich hard-wraps at the terminal width."""
    return " ".join(capsys.readouterr().out.split())


def error_of(func, *args, **kwargs) -> str:
    with pytest.raises(ValueError) as excinfo:
        func(*args, **kwargs)
    return str(excinfo.value)


def test_missing_agent_dir_points_at_init():
    message = error_of(cli.resolve_agent_dir, "nope")
    assert "does not exist" in message and "upshift init nope" in message


def test_no_agent_dir_anywhere_points_at_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    message = error_of(cli.resolve_agent_dir, None)
    assert "no agent directory" in message
    assert "--agent" in message and "upshift init" in message


def test_a_single_agent_dir_in_the_cwd_is_detected(tmp_path, monkeypatch, agent):
    monkeypatch.chdir(tmp_path)
    found, raw = cli.resolve_agent_dir(None)
    assert found.resolve() == agent.resolve()
    assert raw["name"] == "booking-agent"


def test_several_agent_dirs_ask_which_one(tmp_path, monkeypatch, agent):
    cli.main(["init", str(tmp_path / "other")])
    monkeypatch.chdir(tmp_path)
    message = error_of(cli.resolve_agent_dir, None)
    assert "several agent directories" in message and "--agent" in message


def test_runs_root_inside_the_agent_dir_is_refused(agent):
    message = error_of(cli.resolve_agent_dir, str(agent), agent / "runs")
    assert "inside the agent directory" in message and "--runs-root" in message


def test_missing_agent_json_names_the_file_and_init(tmp_path):
    (tmp_path / "empty-dir").mkdir()
    message = error_of(cli.validate_agent_dir, tmp_path / "empty-dir")
    assert "agent.json" in message and "upshift init" in message


def test_malformed_agent_json_names_the_file(agent):
    (agent / "agent.json").write_text('{"name": "x",')
    message = error_of(cli.validate_agent_dir, agent)
    assert "agent.json" in message and "malformed JSON" in message


def test_agent_json_missing_a_key_names_the_key(agent):
    (agent / "agent.json").write_text('{"name": "x", "endpoint": "responses"}')
    assert "missing required key 'model'" in error_of(cli.validate_agent_dir, agent)


def test_unknown_endpoint_lists_the_valid_ones(agent):
    raw = json.loads((agent / "agent.json").read_text())
    raw["endpoint"] = "assistants"
    (agent / "agent.json").write_text(json.dumps(raw))
    message = error_of(cli.validate_agent_dir, agent)
    assert "chat_completions" in message and "responses" in message


def test_missing_prompt_file_names_the_pointer(agent):
    (agent / "system_prompt.txt").unlink()
    message = error_of(cli.validate_agent_dir, agent)
    assert "system_prompt_file" in message and "does not exist" in message


def test_missing_cases_file_says_where_it_belongs(agent):
    (agent / "cases" / "cases.json").unlink()
    message = error_of(cli.validate_agent_dir, agent)
    assert "cases/cases.json" in message


def test_malformed_case_entry_names_the_file(agent):
    (agent / "cases" / "cases.json").write_text('[{"id": "x"}]')
    message = error_of(cli.validate_agent_dir, agent)
    assert "cases.json" in message and "user_messages" in message


def test_empty_case_suite_is_rejected(agent):
    (agent / "cases" / "cases.json").write_text("[]")
    assert "empty" in error_of(cli.validate_agent_dir, agent)


def test_backend_without_create_backend_names_the_file(agent):
    (agent / "backend.py").write_text("x = 1\n")
    message = error_of(cli.validate_agent_dir, agent)
    assert "backend.py" in message and "create_backend" in message


def test_backend_that_fails_to_import_reports_one_line(agent):
    (agent / "backend.py").write_text("import a_module_that_does_not_exist\n")
    message = error_of(cli.validate_agent_dir, agent)
    assert "backend.py" in message and "ModuleNotFoundError" in message


# ---------------------------------------------------------------------------
# Provider / model guards
# ---------------------------------------------------------------------------


def test_sim_provider_rejects_a_real_model_name():
    message = error_of(cli._check_models, "sim", ["gpt-5.5"])
    assert cli.SIM_BASELINE_MODEL in message and cli.SIM_CANDIDATE_MODEL in message


def test_openai_provider_rejects_a_sim_model_name():
    message = error_of(cli._check_models, "openai", ["sim-5.6-sol"])
    assert "--provider sim" in message


def test_sim_models_pass_their_own_check():
    cli._check_models("sim", [cli.SIM_BASELINE_MODEL, cli.SIM_CANDIDATE_MODEL])
    cli._check_models("openai", ["gpt-5.6-sol", None])


def test_missing_api_key_fails_before_any_run(tmp_path, monkeypatch, agent, capsys):
    monkeypatch.chdir(tmp_path)  # away from any .env
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = cli.main(
        ["run", "--agent", str(agent), "--provider", "openai", "--run-id", "r", "--n", "1"]
    )
    assert code == 2
    out = flat(capsys)
    assert "OPENAI_API_KEY" in out and "--provider sim" in out
    assert not (tmp_path / "runs").exists()


def test_zero_reps_is_rejected(tmp_path, monkeypatch, agent, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        [
            "run", "--agent", str(agent), "--provider", "sim", "--model", "sim-5.5",
            "--run-id", "r", "--n", "0",
        ]
    )
    assert code == 2
    assert "--n must be at least 1" in flat(capsys)


def test_nonpositive_repair_budget_is_rejected_before_any_run(
    tmp_path, monkeypatch, agent, capsys
):
    """A negative --budget used to run the whole (paid) pipeline and then repair nothing."""
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        [
            "upgrade", "--agent", str(agent), "--provider", "sim",
            "--baseline-model", cli.SIM_BASELINE_MODEL,
            "--candidate-model", cli.SIM_CANDIDATE_MODEL,
            "--tag", "t", "--budget", "-1",
        ]
    )
    assert code == 2
    out = flat(capsys)
    assert "--budget must be at least 1" in out and "--no-repair" in out
    assert not (tmp_path / "runs").exists()


def test_adapt_without_a_key_does_not_advise_a_provider_flag_it_lacks(
    tmp_path, monkeypatch, capsys
):
    """`adapt` has no --provider and no simulator; the key error must not point at one."""
    monkeypatch.chdir(tmp_path)  # away from any .env
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = cli.main(["adapt", str(tmp_path), "--out", str(tmp_path / "out")])
    assert code == 2
    out = flat(capsys)
    assert "OPENAI_API_KEY" in out
    assert "--provider sim" not in out
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# main(): every exception the CLI can see becomes a message, never a traceback
# ---------------------------------------------------------------------------


def test_every_flag_and_positional_documents_itself(capsys):
    """`--help` is the whole manual for a stranger; a bare `--runs-root RUNS_ROOT` is not one."""
    for sub in ("init", "adapt", "run", "diff", "upgrade", "cost", "report"):
        with pytest.raises(SystemExit):
            cli.main([sub, "--help"])
        # NOT flat(): this needs argparse's line structure, one entry per line.
        lines = capsys.readouterr().out.splitlines()
        undocumented = []
        for i, line in enumerate(lines):
            # an entry line: two spaces of indent, then the flag/positional and its metavar
            if not (line.startswith("  ") and not line.startswith("    ") and line.strip()):
                continue
            if len(line.split()) > 2 or line.strip().startswith("-h"):
                continue  # the description shares the line, or it is argparse's own --help
            # argparse wraps a long flag: the description lands on the next, deeper-indented
            # line. Nothing there means the flag ships without one.
            following = lines[i + 1] if i + 1 < len(lines) else ""
            if not (following.startswith("        ") and following.strip()):
                undocumented.append(line.split()[0])
        assert not undocumented, f"{sub}: undocumented {undocumented}"


def test_bare_invocation_prints_help_and_the_starting_command(capsys):
    assert cli.main([]) == 0
    out = flat(capsys)
    assert "upshift init" in out


def test_keyboard_interrupt_says_runs_resume(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_report", _raise(KeyboardInterrupt))
    assert cli.main(["report", "x.json"]) == 130
    assert "resume" in flat(capsys)


def test_provider_api_error_is_a_message(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_report", _raise(ProviderAPIError("upstream is down", 503)))
    assert cli.main(["report", "x.json"]) == 2
    assert "upstream is down" in flat(capsys)


def test_os_error_is_a_message(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_report", _raise(PermissionError(13, "Permission denied")))
    assert cli.main(["report", "x.json"]) == 2
    assert "Permission denied" in flat(capsys)


def test_error_text_with_brackets_does_not_break_rendering(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_report", _raise(ValueError("unknown case ids: ['nope']")))
    assert cli.main(["report", "x.json"]) == 2
    assert "['nope']" in flat(capsys)


def test_report_on_a_missing_file_explains_where_diffs_live(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["report", "nope.json"]) == 2
    assert "diff.json" in flat(capsys)


def test_cost_without_a_runs_directory_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["cost"]) == 2
    assert "nothing has been recorded yet" in flat(capsys)


def _raise(exc):
    def boom(_args):
        raise exc

    return boom


# ---------------------------------------------------------------------------
# End to end on the scaffolded agent (sim provider, two cases, one rep)
# ---------------------------------------------------------------------------


def _trim_to_two_stable_cases(agent_dir: Path) -> list[str]:
    cases = json.loads((agent_dir / "cases" / "cases.json").read_text())
    stable = [c for c in cases if not c["sim"].get("vulnerable_to") and "flaky" not in c["sim"]]
    keep = stable[:2]
    (agent_dir / "cases" / "cases.json").write_text(json.dumps(keep))
    return [c["id"] for c in keep]


def test_scaffolded_agent_runs_end_to_end_on_the_simulator(tmp_path, monkeypatch, agent, capsys):
    monkeypatch.chdir(tmp_path)
    case_ids = _trim_to_two_stable_cases(agent)
    code = cli.main(
        [
            "run", "--agent", str(agent), "--provider", "sim", "--model", cli.SIM_BASELINE_MODEL,
            "--run-id", "smoke", "--n", "1", "--quiet",
        ]
    )
    assert code == 0
    summary = json.loads((tmp_path / "runs" / "smoke" / "summary.json").read_text())
    assert sorted(summary) == sorted(case_ids)
    assert all(s["passes"] == s["n"] == 1 for s in summary.values())
    assert "smoke" in flat(capsys)


def test_upgrade_pipeline_produces_a_patch_and_a_verdict(tmp_path, monkeypatch, agent):
    monkeypatch.chdir(tmp_path)
    _trim_to_two_stable_cases(agent)
    code = cli.main(
        [
            "upgrade", "--agent", str(agent), "--provider", "sim",
            "--baseline-model", cli.SIM_BASELINE_MODEL,
            "--candidate-model", cli.SIM_CANDIDATE_MODEL,
            "--tag", "demo", "--n", "1", "--quiet",
        ]
    )
    out_dir = tmp_path / "runs" / "demo"
    verdict = json.loads((out_dir / "verdict.json").read_text())
    assert verdict["verdict"] == "SAFE WITH PATCH"
    assert verdict["provider"] == "sim"
    assert not verdict["unrestored"]
    assert code == 0
    patch = (out_dir / "upgrade.patch").read_text()
    assert patch.startswith(f"diff --git a/{agent.name}/") or f"a/{agent.name}/" in patch
    assert (out_dir / "REPORT.md").read_text().strip()


def test_toolless_agent_is_accepted(agent):
    """A plain completion agent — one system prompt, no tool loop — is a plain API agent.
    Real targets look like this (laude-institute/headlong's `bin/llm` POSTs to /v1/messages
    and prints the text; it defines no tools), and the loop already handles an empty tool
    list. Only a non-list is an authoring error."""
    (agent / "tools.json").write_text("[]")
    raw = cli.validate_agent_dir(agent)
    assert raw["tools_file"] == "tools.json"


def test_tools_file_that_is_not_a_list_is_still_rejected(agent):
    (agent / "tools.json").write_text('{"not": "a list"}')
    message = error_of(cli.validate_agent_dir, agent)
    assert "tools.json" in message and "JSON list" in message
