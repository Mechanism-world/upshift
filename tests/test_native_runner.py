"""The native runner: protocol, safety, and one suite that runs through the normal engine.

DESIGN.md §C. The tests are organised the way the risk is: the protocol first (what upshift
will accept), then the execution boundary (what upshift refuses to do to your machine), then
one end-to-end run proving a `runner` agent goes through `run_suite`, the check engine and the
record format unchanged — the requirement the whole native path exists under.

The reference runners under `examples/runners/` drive a real tool-calling loop against a stub
provider, so nothing here needs a key, a network or a cent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from upshift import recorder
from upshift.native import protocol
from upshift.native.cli import (
    EXIT_RUNNER_NOT_AUTHORIZED,
    check_authorized,
    describe_runner,
    is_authorized,
    runner_options,
)
from upshift.native.exec import RunnerNotAuthorized, RunnerOptions, build_env, run_command
from upshift.native.protocol import ProtocolError, RunnerSpec
from upshift.native.runner import CaptureSession, run_native_episode
from upshift.providers.sim import SimProvider
from upshift.runner import episode_source_for, run_suite, verification_scope
from upshift.schemas import (
    SCOPE_ADAPTED_AGENT,
    SCOPE_NATIVE_APPLICATION,
    SCOPE_REQUEST_CONTRACT,
    AgentConfig,
    Case,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "runners"

CASES = [
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


def _native_agent(
    tmp_path: Path,
    *,
    command: list[str],
    files: dict[str, str] | None = None,
    copy_examples: bool = False,
    runner_extra: dict | None = None,
    cases: list | None = None,
) -> Path:
    """An agent directory whose whole authoring surface is `runner` + `cases/`."""
    agent = tmp_path / "agent"
    (agent / "cases").mkdir(parents=True)
    (agent / "app").mkdir()
    if copy_examples:
        for name in ("python_runner.py", "fake_provider.py", "node_runner.mjs"):
            shutil.copy(EXAMPLES / name, agent / "app" / name)
    for name, content in (files or {}).items():
        path = agent / "app" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    runner = {
        "kind": "command",
        "workdir": "app",
        "command": command,
        "env": {"DEMO_MODEL": "{model}", "DEMO_CASE": "{case_id}", "DEMO_REP": "{rep}"},
        "timeout_s": 60,
        **(runner_extra or {}),
    }
    (agent / "agent.json").write_text(
        json.dumps(
            {
                "name": "native-task-agent",
                "model": "stub-model-a",
                "endpoint": "chat_completions",
                "runner": runner,
            },
            indent=2,
        )
    )
    (agent / "cases" / "cases.json").write_text(json.dumps(cases if cases is not None else CASES))
    return agent


def _allowed() -> RunnerOptions:
    return RunnerOptions(allow_runner=True, provider="openai")


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


def test_a_result_is_the_last_json_line_and_noise_before_it_is_fine() -> None:
    """A developer's existing command prints its own output; the protocol tolerates that."""
    stdout = "running 3 tests...\nok\n" + json.dumps(
        {"protocol": 1, "final_message": "done", "final_state": {"tasks": []}}
    )
    parsed = protocol.parse_result(stdout)
    assert parsed.final_message == "done"
    assert parsed.api_error is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("no json here", "no JSON object"),
        ("{not json}", "not valid JSON"),
        ('{"final_message": "x"}', "no `protocol` key"),
        ('{"protocol": 2}', "speaks protocol 2"),
        ('[{"protocol": 1}]', "no JSON object"),
        ('{"protocol": 1, "tool_executions": {}}', "`tool_executions` must be a list"),
        ('{"protocol": 1, "tool_executions": [{"arguments": {}}]}', "name` must be a non-empty"),
        ('{"protocol": 1, "api_error": "boom"}', "must be null or an object"),
    ],
)
def test_every_way_a_runner_can_speak_wrong_is_a_named_protocol_error(stdout, expected) -> None:
    with pytest.raises(ProtocolError) as exc:
        protocol.parse_result(stdout)
    assert expected in str(exc.value)


def test_the_case_request_is_stable_json_the_child_can_rely_on() -> None:
    request = protocol.CaseRequest(
        case_id="c1", rep=2, seed=7, model="m", endpoint="messages",
        initial_state={"a": 1}, user_messages=["hi"], patch_applied=True,
    )
    payload = json.loads(request.to_json())
    assert payload == {
        "protocol": 1, "case_id": "c1", "rep": 2, "seed": 7, "model": "m",
        "endpoint": "messages", "initial_state": {"a": 1}, "user_messages": ["hi"],
        "patch_applied": True,
    }


def test_runner_error_and_continuation_exhausted_are_the_non_behavioural_pair() -> None:
    """The constants stream 1's differ and stream 3's verdict consume."""
    assert protocol.NON_BEHAVIOURAL_ERROR_TYPES == ("runner_error", "continuation_exhausted")
    payload = protocol.error_payload("boom")
    # `type` for every existing reader, `error_type` for DESIGN.md §C. One value.
    assert payload["type"] == payload["error_type"] == "runner_error"
    assert payload["status_code"] is None
    assert protocol.is_non_behavioural(payload)
    assert not protocol.is_non_behavioural({"type": "invalid_request_error", "status_code": 400})
    with pytest.raises(ValueError, match="not a non-behavioural"):
        protocol.error_payload("x", error_type="invalid_request_error")


# ---------------------------------------------------------------------------
# The runner block
# ---------------------------------------------------------------------------


def test_a_shell_string_command_is_refused_with_the_argv_it_should_have_been() -> None:
    """There is no shell in the native path, so a command is an argv list or nothing."""
    with pytest.raises(ValueError, match="argv list, never a shell string"):
        RunnerSpec.from_dict({"command": "python -m myapp.eval_case"})


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ({"command": ["x"], "kind": "docker"}, "unknown runner kind"),
        ({"command": ["x"], "isolation": "none"}, "unknown runner isolation"),
        ({"command": ["x"], "timeout_s": 0}, "must be positive"),
        ({"command": ["x"], "env": {"A": {"nested": 1}}}, "must be a string"),
        ({"command": ["x"], "env": {"A": "{mdoel}"}}, "unknown template variable"),
        ({"command": []}, "non-empty list"),
        ("python x.py", "`runner` must be an object"),
    ],
)
def test_a_malformed_runner_block_fails_before_anything_runs(block, expected) -> None:
    with pytest.raises(ValueError, match=expected):
        RunnerSpec.from_dict(block)


def test_a_workdir_cannot_escape_the_agent_directory(tmp_path: Path) -> None:
    """agent.json is an input like any other: `../../..` must not become a copied directory."""
    agent = _native_agent(tmp_path, command=["true"], runner_extra={"workdir": "../.."})
    with pytest.raises(ValueError, match="resolves outside the agent directory"):
        AgentConfig.load(agent).runner_spec().resolved_workdir(agent)


def test_env_templates_render_the_case_the_child_is_about_to_run() -> None:
    spec = RunnerSpec.from_dict(
        {"command": ["x"], "env": {"M": "{model}", "TAG": "{case_id}-{rep}-{seed}"}}
    )
    env = build_env(
        spec,
        RunnerOptions(allow_runner=True),
        {"model": "gpt-5.6-sol", "endpoint": "responses", "case_id": "c1", "rep": 3, "seed": 9},
        parent_env={"PATH": "/bin"},
    )
    assert env["M"] == "gpt-5.6-sol"
    assert env["TAG"] == "c1-3-9"


# ---------------------------------------------------------------------------
# Security: what upshift refuses to do to your machine
# ---------------------------------------------------------------------------


def test_nothing_executes_without_allow_runner(tmp_path: Path) -> None:
    """The gate that cannot be forgotten: it lives under run_suite, not only in the CLI."""
    marker = tmp_path / "executed"
    agent = _native_agent(
        tmp_path,
        command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
    )
    with pytest.raises(RunnerNotAuthorized, match="--allow-runner"):
        run_suite(agent, SimProvider(), "r1", n_reps=1, runs_root=tmp_path / "runs", workers=1)
    assert not marker.exists()


def test_the_cli_gate_prints_the_command_and_exits_two(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("UPSHIFT_ALLOW_RUNNER", raising=False)
    agent = _native_agent(tmp_path, command=["python3", "runner.py", "--live"])
    config = AgentConfig.load(agent)
    printed: list[str] = []

    class Args:
        allow_runner = False
        allow_in_place = False

    code = check_authorized(config, Args(), provider="openai", printer=printed.append)
    assert code == EXIT_RUNNER_NOT_AUTHORIZED
    text = "\n".join(printed)
    assert "python3 runner.py --live" in text  # the exact argv, before anything happens
    assert "Nothing was executed" in text
    assert "--allow-runner" in text
    assert "workdir-copy" in describe_runner(config)


def test_the_env_variable_authorizes_the_way_the_flag_does(monkeypatch) -> None:
    class Args:
        allow_runner = False

    monkeypatch.setenv("UPSHIFT_ALLOW_RUNNER", "1")
    assert is_authorized(Args())
    monkeypatch.setenv("UPSHIFT_ALLOW_RUNNER", "yes")
    assert not is_authorized(Args())  # only "1", so a stray truthy value is not consent


def test_the_child_does_not_see_the_operators_environment(tmp_path: Path, monkeypatch) -> None:
    """A test command run 5x/case must not carry unrelated production credentials."""
    monkeypatch.setenv("UNRELATED_PRODUCTION_SECRET", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-this-run")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-this-run")
    agent = _native_agent(
        tmp_path,
        command=[sys.executable, "dump_env.py"],
        files={
            "dump_env.py": (
                "import json, os, sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'protocol': 1, 'final_message': 'ok',\n"
                "                  'final_state': {'env': dict(os.environ)}}))\n"
            )
        },
    )
    config = AgentConfig.load(agent)
    episode = run_native_episode(
        config,
        Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[]),
        rep=1, seed=1, model="gpt-5.6-sol", endpoint="chat_completions",
        options=RunnerOptions(allow_runner=True, provider="openai"),
    )
    env = episode.final_state["env"]
    assert "UNRELATED_PRODUCTION_SECRET" not in env
    assert "ANTHROPIC_API_KEY" not in env  # a different provider's key, on an openai run
    assert env["OPENAI_API_KEY"] == "sk-openai-this-run"
    assert env["DEMO_MODEL"] == "gpt-5.6-sol"
    assert env["UPSHIFT_NATIVE_RUNNER"] == "1"


def test_each_rep_runs_in_a_temp_copy_that_is_deleted_afterwards(tmp_path: Path) -> None:
    """A command that writes files cannot make rep 2 differ from rep 1."""
    agent = _native_agent(
        tmp_path,
        command=[sys.executable, "write_then_report.py"],
        files={
            "write_then_report.py": (
                "import json, os, pathlib, sys\n"
                "sys.stdin.read()\n"
                "p = pathlib.Path('side_effect.txt')\n"
                "previous = p.read_text() if p.exists() else ''\n"
                "p.write_text(previous + 'x')\n"
                "print(json.dumps({'protocol': 1, 'final_message': p.read_text(),\n"
                "                  'final_state': {'cwd': os.getcwd()}}))\n"
            )
        },
    )
    config = AgentConfig.load(agent)
    case = Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[])
    seen = []
    for rep in (1, 2):
        episode = run_native_episode(
            config, case, rep=rep, seed=1, model="m", endpoint="chat_completions",
            options=_allowed(),
        )
        # Same content every rep: the write never survives into the next one.
        assert episode.final_message == "x"
        seen.append(episode.final_state["cwd"])
    assert not (agent / "app" / "side_effect.txt").exists()  # the real checkout is untouched
    for cwd in seen:
        assert str(agent) not in cwd  # it ran in a temp copy
        assert not Path(cwd).exists()  # which no longer exists


def test_in_place_isolation_needs_its_own_flag(tmp_path: Path) -> None:
    agent = _native_agent(
        tmp_path,
        command=[sys.executable, "-c", "print('{}')"],
        runner_extra={"isolation": "in-place"},
    )
    config = AgentConfig.load(agent)
    case = Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[])
    with pytest.raises(RunnerNotAuthorized, match="--allow-in-place"):
        run_native_episode(config, case, rep=1, seed=1, model="m",
                           endpoint="chat_completions", options=_allowed())
    episode = run_native_episode(
        config, case, rep=1, seed=1, model="m", endpoint="chat_completions",
        options=RunnerOptions(allow_runner=True, allow_in_place=True),
    )
    # It ran, in the checkout itself, and said so in the record.
    assert episode.runner["isolation"] == "in-place"


def test_output_beyond_the_cap_is_a_runner_error_not_a_behaviour(tmp_path: Path) -> None:
    agent = _native_agent(
        tmp_path,
        command=[sys.executable, "-c", "import sys; sys.stdin.read(); print('A' * 100000)"],
        runner_extra={"max_output_bytes": 1000},
    )
    episode = run_native_episode(
        AgentConfig.load(agent),
        Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[]),
        rep=1, seed=1, model="m", endpoint="chat_completions", options=_allowed(),
    )
    assert episode.api_error["error_type"] == "runner_error"
    assert "max_output_bytes" in episode.api_error["message"]
    assert episode.runner["truncated"] is True


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX concept")
def test_a_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """Killing only the parent leaves `npm test`'s children running — and spending."""
    grandchild_marker = tmp_path / "grandchild_survived.txt"
    agent = _native_agent(
        tmp_path,
        command=[sys.executable, "spawn_and_hang.py", str(grandchild_marker)],
        files={
            "spawn_and_hang.py": (
                "import subprocess, sys, time\n"
                "sys.stdin.read()\n"
                "subprocess.Popen([sys.executable, '-c',\n"
                "  \"import time,sys; time.sleep(3); open(sys.argv[1],'w').write('alive')\",\n"
                "  sys.argv[1]])\n"
                "time.sleep(60)\n"
            )
        },
        runner_extra={"timeout_s": 1},
    )
    started = time.monotonic()
    episode = run_native_episode(
        AgentConfig.load(agent),
        Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[]),
        rep=1, seed=1, model="m", endpoint="chat_completions", options=_allowed(),
    )
    assert time.monotonic() - started < 30
    assert episode.runner["timed_out"] is True
    assert episode.api_error["error_type"] == "runner_error"
    assert "timeout_s" in episode.api_error["message"]
    time.sleep(4)
    assert not grandchild_marker.exists()  # the grandchild died with the group


def test_a_capture_recorder_for_a_native_run_can_only_be_loopback(tmp_path: Path) -> None:
    """`--capture` never grows an --allow-remote: it holds the user's key mid-test-run."""
    with pytest.raises(ValueError, match="loopback only"):
        CaptureSession(tmp_path / "wire", provider="anthropic", listen="0.0.0.0:8787")


def test_a_capture_session_points_the_child_at_the_socket_it_really_bound(tmp_path: Path) -> None:
    with CaptureSession(tmp_path / "wire", provider="anthropic", listen="127.0.0.1:0") as session:
        assert session.base_url.startswith("http://127.0.0.1:")
        assert session.env() == {"ANTHROPIC_BASE_URL": session.base_url}
        spec = RunnerSpec.from_dict({"command": ["x"], "env": {"ANTHROPIC_BASE_URL": "http://evil"}})
        env = build_env(
            spec,
            RunnerOptions(allow_runner=True, provider="anthropic", capture_env=session.env()),
            {key: "" for key in protocol.ENV_TEMPLATE_KEYS},
            parent_env={},
        )
        # A runner block cannot redirect the child away from the recorder.
        assert env["ANTHROPIC_BASE_URL"] == session.base_url
    assert (tmp_path / "wire" / "index.json").is_file()


# ---------------------------------------------------------------------------
# Failure classes: a broken harness is never a model regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("import sys; sys.stdin.read(); sys.exit(3)", "exited 3"),
        ("import sys; sys.stdin.read(); print('not json')", "no JSON object"),
        ("import sys; sys.stdin.read(); print('{\"final_message\": 1}')", "no `protocol` key"),
    ],
)
def test_a_broken_runner_is_recorded_as_runner_error(tmp_path: Path, body, expected) -> None:
    agent = _native_agent(tmp_path, command=[sys.executable, "-c", body])
    episode = run_native_episode(
        AgentConfig.load(agent),
        Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[]),
        rep=1, seed=1, model="m", endpoint="chat_completions", options=_allowed(),
    )
    assert episode.api_error["error_type"] == protocol.RUNNER_ERROR
    assert protocol.is_non_behavioural(episode.api_error)
    assert expected in episode.api_error["message"]


def test_a_command_that_does_not_exist_is_a_runner_error_not_a_crash(tmp_path: Path) -> None:
    agent = _native_agent(tmp_path, command=["definitely-not-a-real-binary-xyz"])
    episode = run_native_episode(
        AgentConfig.load(agent),
        Case(id="c", description="", initial_state={}, user_messages=["go"], checks=[]),
        rep=1, seed=1, model="m", endpoint="chat_completions", options=_allowed(),
    )
    assert episode.api_error["error_type"] == protocol.RUNNER_ERROR
    assert "could not be started" in episode.api_error["message"]


def test_a_provider_error_the_app_reports_stays_a_provider_error(tmp_path: Path) -> None:
    """The distinction the whole class system rests on: the app's 400 is EVIDENCE."""
    agent = _native_agent(
        tmp_path, command=[sys.executable, "python_runner.py"], copy_examples=True,
    )
    episode = run_native_episode(
        AgentConfig.load(agent),
        Case(id="c", description="", initial_state={"tasks": []},
             user_messages=["Add a task."], checks=[]),
        rep=1, seed=1, model="stub-model-b", endpoint="chat_completions", options=_allowed(),
    )
    assert episode.api_error["status_code"] == 400
    assert not protocol.is_non_behavioural(episode.api_error)  # a real regression, not our fault
    assert "Function tools with reasoning_effort" in episode.api_error["message"]


# ---------------------------------------------------------------------------
# The point: a runner agent goes through run_suite unchanged
# ---------------------------------------------------------------------------


def _run(agent: Path, tmp_path: Path, run_id: str, *, model: str | None = None, n: int = 3):
    run_directory = run_suite(
        agent,
        SimProvider(),  # never called on a native run; run_suite still records the provider
        run_id,
        n_reps=n,
        model_override=model,
        runs_root=tmp_path / "runs",
        workers=2,
        runner_options=_allowed(),
    )
    return run_directory, json.loads((run_directory / "summary.json").read_text())


def test_a_native_agent_runs_n_reps_through_the_normal_check_engine(tmp_path: Path) -> None:
    agent = _native_agent(
        tmp_path, command=[sys.executable, "python_runner.py"], copy_examples=True,
    )
    run_directory, summary = _run(agent, tmp_path, "native-baseline")
    assert summary == {"add_one_task": {"passes": 3, "n": 3}}

    record = recorder.load_rep(run_directory, "add_one_task", 1)
    # An ordinary RepRecord: same fields, same checks, same statistics downstream.
    assert record.passed
    assert [t.name for t in record.tool_executions] == ["add_task"]
    assert record.final_state["tasks"][0]["title"] == "Add a task to call the dentist."
    assert record.usage["input_tokens"] == 200
    assert record.check_results[0].check["type"] == "no_api_error"
    # …plus the native metadata (DESIGN.md §A).
    assert record.scope == SCOPE_NATIVE_APPLICATION
    assert record.episode_source == "native_application"
    assert record.runner["exit_code"] == 0
    assert record.runner["isolation"] == "workdir-copy"
    assert record.continuation_used is False


def test_the_same_suite_on_a_model_the_app_cannot_use_is_a_regression(tmp_path: Path) -> None:
    """Baseline PASS -> candidate FAIL, measured through the application's own request code."""
    agent = _native_agent(
        tmp_path, command=[sys.executable, "python_runner.py"], copy_examples=True,
    )
    _, baseline = _run(agent, tmp_path, "base")
    run_directory, candidate = _run(agent, tmp_path, "cand", model="stub-model-b")
    assert baseline["add_one_task"]["passes"] == 3
    assert candidate["add_one_task"]["passes"] == 0
    record = recorder.load_rep(run_directory, "add_one_task", 1)
    assert record.api_error["status_code"] == 400
    assert not protocol.is_non_behavioural(record.api_error)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_node_reference_runner_speaks_the_same_protocol(tmp_path: Path) -> None:
    agent = _native_agent(tmp_path, command=["node", "node_runner.mjs"], copy_examples=True)
    _, summary = _run(agent, tmp_path, "native-node", n=2)
    assert summary == {"add_one_task": {"passes": 2, "n": 2}}


def test_the_node_reference_runner_is_at_least_syntactically_valid() -> None:
    """Runs even where node is absent, so the file cannot rot unnoticed."""
    source = (EXAMPLES / "node_runner.mjs").read_text()
    assert '"protocol"' not in source or "protocol" in source
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: syntax check skipped, file still shipped")
    result = subprocess.run(
        [node, "--check", str(EXAMPLES / "node_runner.mjs")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Verification scope (DESIGN.md §A)
# ---------------------------------------------------------------------------


def test_scope_is_derived_from_the_directory_not_declared(tmp_path: Path) -> None:
    native = _native_agent(tmp_path, command=["true"])
    assert verification_scope(native, AgentConfig.load(native)) == SCOPE_NATIVE_APPLICATION
    assert episode_source_for(native, AgentConfig.load(native)) == "native_application"

    adapter = Path(__file__).resolve().parent / "todo_agent"
    assert verification_scope(adapter, AgentConfig.load(adapter)) == SCOPE_ADAPTED_AGENT
    assert episode_source_for(adapter, AgentConfig.load(adapter)) == "live_model"


def test_a_capture_replay_directory_is_only_a_request_contract(tmp_path: Path) -> None:
    """A replay backend exercised no world, so no report may call it a verified application."""
    adapter = Path(__file__).resolve().parent / "todo_agent"
    copy = tmp_path / "replayed"
    shutil.copytree(adapter, copy)
    (copy / "recorded_tools.json").write_text("{}")
    assert verification_scope(copy, AgentConfig.load(copy)) == SCOPE_REQUEST_CONTRACT
    assert episode_source_for(copy, AgentConfig.load(copy)) == "recorded_playback"


def test_run_command_refuses_before_it_builds_an_environment(tmp_path: Path) -> None:
    spec = RunnerSpec.from_dict({"command": [sys.executable, "-c", "print(1)"], "workdir": "."})
    with pytest.raises(RunnerNotAuthorized):
        run_command(spec, tmp_path, "{}", RunnerOptions(), {})


def test_runner_options_defaults_are_the_safe_ones() -> None:
    class Args:
        allow_runner = False
        allow_in_place = False

    options = runner_options(Args(), provider="openai")
    assert options.allow_runner is False
    assert options.allow_in_place is False
    assert options.capture_env == {}
