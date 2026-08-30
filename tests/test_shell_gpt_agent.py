"""Mechanical validation of the shell_gpt proof-target agent directory.

No API key is used and no API call is made anywhere in this file. What is proved here:

1. The directory satisfies the ADAPTER.md contract (agent.json/prompt/tools load through the
   real loaders; backend.py loads through runner.load_backend_factory).
2. The extracted prompt and tool schema are byte-identical to what upstream `sgpt` renders.
3. The sandbox works: a fixture tree materializes, commands round-trip through Docker, and
   the same command sequence twice over the same initial_state gives identical output and
   identical state.
4. The check suite lints against the real engine, and — for a sample of cases — synthetic
   episodes exercise both the passing and the failing path of the case's own checks.

The Docker-backed tests skip (never fail) when the sandbox image is absent, so the suite is
green on a machine that has not built it; the pure-fixture tests always run.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift.checks import CHECK_TYPES, evaluate_checks
from upshift.runner import load_backend_factory
from upshift.schemas import AgentConfig, Case, ToolExecution

AGENT_DIR = ROOT / "agents" / "shell_gpt"
CASES = Case.load_all(AGENT_DIR / "cases" / "cases.json")
BY_ID = {case.id: case for case in CASES}
CONFIG = AgentConfig.load(AGENT_DIR)

# The runner's own loader, exercised exactly as run_suite would (see the contract test below).
LOADED_FACTORY = load_backend_factory(AGENT_DIR)

# A named handle on the same module, so tests can reach module-level knobs (TIMEOUT_S) that
# runner.load_backend_factory deliberately does not register in sys.modules.
_SPEC = importlib.util.spec_from_file_location("shell_gpt_backend", AGENT_DIR / "backend.py")
BACKEND = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BACKEND)
create_backend = BACKEND.create_backend

SANDBOX_OK, SANDBOX_WHY = BACKEND.sandbox_available()
needs_sandbox = pytest.mark.skipif(not SANDBOX_OK, reason=f"shellbox sandbox: {SANDBOX_WHY}")

EXPECTED_CASE_COUNT = 14
TOOL_NAME = "execute_shell_command"

# Rendered by sgpt as ROLE_TEMPLATE.format(name="ShellGPT", role=DEFAULT_ROLE.format(
# os="Linux", shell="bash")) at commit a082bd5327ce0c4ef5a0284d9060e833be9444a6.
UPSTREAM_PROMPT = (
    "You are ShellGPT\n"
    "You are programming and system administration assistant.\n"
    "You are managing Linux operating system with bash shell.\n"
    "Provide short responses in about 100 words, unless you are specifically asked for more "
    "details.\n"
    "If you need to store any data, assume it will be stored in the conversation.\n"
    "APPLY MARKDOWN formatting when possible."
)

# Function.openai_schema() at the same commit, verbatim (pydantic's `title`/`example` keys
# included — sgpt sends those to OpenAI too).
UPSTREAM_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Executes a shell command and returns the output (result).",
        "parameters": {
            "type": "object",
            "properties": {
                "shell_command": {
                    "description": "Shell command to execute.",
                    "example": "ls -la",
                    "title": "Shell Command",
                    "type": "string",
                }
            },
            "required": ["shell_command"],
        },
    },
}


def ids(cases):
    return [c.id for c in cases]


@pytest.fixture(params=CASES, ids=ids(CASES))
def case(request) -> Case:
    return request.param


# ---------------------------------------------------------------------------
# The agent directory contract (ADAPTER.md)
# ---------------------------------------------------------------------------


def test_agent_config_loads_through_the_real_loader():
    assert CONFIG.name == "shell-gpt"
    assert CONFIG.endpoint == "chat_completions"
    assert CONFIG.model == "gpt-5.5"
    assert CONFIG.max_turns == 6
    # sgpt has no reasoning_effort knob at all. An empty params block is the point of this
    # target: the documented gpt-5.6 break cannot be configured away here.
    assert CONFIG.params == {}


def test_system_prompt_is_byte_identical_to_upstream():
    assert (AGENT_DIR / "system_prompt.txt").read_bytes() == UPSTREAM_PROMPT.encode()
    assert CONFIG.system_prompt == UPSTREAM_PROMPT


def test_tool_schema_is_byte_identical_to_upstream_and_is_the_only_tool():
    assert CONFIG.tools == [UPSTREAM_TOOL]


def test_runners_backend_loader_produces_a_working_backend():
    """runner.load_backend_factory is what run_suite calls; it must find create_backend here
    and build a backend whose state matches the module-level import used by these tests."""
    case = BY_ID["read_largest_file"]
    assert LOADED_FACTORY(case.initial_state).state() == create_backend(case.initial_state).state()


def test_agent_dir_has_every_file_the_contract_requires():
    for name in ("agent.json", "system_prompt.txt", "tools.json", "backend.py", "ATTRIBUTION.md"):
        assert (AGENT_DIR / name).is_file(), name
    assert (AGENT_DIR / "cases" / "cases.json").is_file()
    assert CONFIG.file_hashes().keys() == {"agent.json", "system_prompt.txt", "tools.json"}


def test_attribution_records_the_commit_the_prompt_came_from():
    text = (AGENT_DIR / "ATTRIBUTION.md").read_text()
    assert "a082bd5327ce0c4ef5a0284d9060e833be9444a6" in text
    assert "github.com/TheR1D/shell_gpt" in text
    assert "MIT" in text


# ---------------------------------------------------------------------------
# Full-suite lint: every case, against the real engine
# ---------------------------------------------------------------------------


def test_suite_shape():
    assert len(CASES) == EXPECTED_CASE_COUNT
    assert len(set(ids(CASES))) == EXPECTED_CASE_COUNT
    for case_id in ids(CASES):
        assert re.fullmatch(r"[a-z0-9_]+", case_id), case_id
    families = {"read": 0, "fidelity": 0, "write": 0, "guard": 0}
    for case_id in ids(CASES):
        families[case_id.split("_", 1)[0]] += 1
    assert families == {"read": 8, "fidelity": 2, "write": 2, "guard": 2}


def test_case_is_well_formed(case):
    assert case.description.strip()
    assert case.user_messages and all(m.strip() for m in case.user_messages)
    assert case.checks[0] == {"type": "no_api_error"}
    assert case.sim == {}, "this agent never runs against the sim provider (no oracle plans)"
    files = case.initial_state["files"]
    assert files and all(isinstance(v, str) and v for v in files.values())
    for relpath in files:
        assert not relpath.startswith("/") and ".." not in Path(relpath).parts, relpath


def test_case_checks_use_only_the_engines_vocabulary(case):
    allowed = {
        "no_api_error",
        "tool_called",
        "tool_not_called",
        "final_state",
        "response_contains",
        "response_not_contains",
        "response_matches",
    }
    for check in case.checks:
        assert check["type"] in allowed, (case.id, check)
        assert check["type"] in CHECK_TYPES, (case.id, check)
        if check["type"] == "tool_called":
            assert check["name"] == TOOL_NAME
            assert 1 <= check["min_times"] <= check["max_times"]
        if check["type"] == "tool_not_called":
            assert check["name"] == TOOL_NAME
        if check["type"] == "final_state":
            assert check["path"] in {"paths", "files", "files_text", "tree_sha256"}, check
        if check["type"] == "response_matches":
            re.compile(check["regex"])


def test_case_initial_state_materializes_and_its_state_check_is_satisfiable(case):
    """Every case's fixture builds, and every final_state check it declares is one the
    backend's own state() can produce (the tree-unchanged ones must match right away)."""
    state = create_backend(case.initial_state).state()
    assert set(state) == {"paths", "files", "files_text", "tree_sha256"}
    assert state["paths"] == sorted(case.initial_state["files"])
    for check in case.checks:
        if check["type"] != "final_state":
            continue
        if check["path"] == "tree_sha256":
            assert state["tree_sha256"] == check["equals"], case.id
        elif check["path"] == "paths":
            # a write case: the expected paths are the fixture plus the files it must create
            created = set(check["equals"]) - set(state["paths"])
            assert created, case.id
            assert set(state["paths"]) < set(check["equals"]), case.id
        elif check["path"] == "files_text":
            # the fixture must start with no small text files, so the check isolates the
            # file the model is asked to write
            assert state["files_text"] == {}, case.id
            expected_paths = next(
                c["equals"] for c in case.checks if c.get("path") == "paths"
            )
            assert set(check["equals"]) <= set(expected_paths), case.id


def test_every_check_is_evaluable_by_the_real_engine(case):
    """checks.py never crashes a run: an unknown type or a malformed check comes back as a
    failed result with a reason. That is exactly the failure mode this lint has to catch, so
    run every check of every case through the engine and reject those two details."""
    state = create_backend(case.initial_state).state()
    results, _ = evaluate_checks(
        case,
        api_error=None,
        tool_executions=[
            ToolExecution(
                turn=0,
                segment=0,
                name=TOOL_NAME,
                arguments={"shell_command": "ls"},
                result={"output": "Exit code: 0, Output:\n"},
            )
        ],
        final_state=state,
        final_message="placeholder answer",
    )
    assert len(results) == len(case.checks)
    for result in results:
        assert "unknown check type" not in result.detail, (case.id, result.detail)
        assert "could not be evaluated" not in result.detail, (case.id, result.detail)


def test_state_manifest_ignores_mtimes_and_tempdir_names(case):
    """Two backends built from the same initial_state are indistinguishable through state()."""
    assert create_backend(case.initial_state).state() == create_backend(case.initial_state).state()


def test_fixture_invariants_the_fidelity_cases_depend_on():
    timeout_case = BY_ID["fidelity_grep_timeout_line"]
    files = timeout_case.initial_state["files"]
    lines = files["logs/app-03.log"].splitlines()
    assert len(lines) == 420
    assert "TIMEOUT" in lines[411]  # line 412, 1-based
    assert sum(line.count("TIMEOUT") for line in lines) == 1
    assert not any("412" in line for line in lines), "the line number must not be readable text"
    for name, body in files.items():
        if name != "logs/app-03.log":
            assert "TIMEOUT" not in body, name

    exports = BY_ID["fidelity_report_filename"].initial_state["files"]
    assert len([n for n in exports if n.endswith(".csv")]) == 1
    assert "exports/RUN_2026-Q3.csv" in exports


NUMERIC_ANSWER_RE = re.compile(r"^\\b\d+\\b$")


def test_a_numeric_answer_is_never_readable_off_the_fixture(case):
    """The counting cases assert their answer with ``\\b<n>\\b``, not ``response_contains``.

    If the number also appeared as a standalone token in a file body, in a filename, or as a
    file's byte size, a model that merely pasted `ls -l` or `tail` output would pass without
    counting anything, and the check would measure transcription instead of work.
    """
    files = case.initial_state["files"]
    haystack = list(files) + list(files.values()) + [str(len(b.encode())) for b in files.values()]
    for check in case.checks:
        if check["type"] != "response_matches" or not NUMERIC_ANSWER_RE.match(check["regex"]):
            continue
        compiled = re.compile(check["regex"])
        for text in haystack:
            hit = compiled.search(text)
            assert hit is None, (case.id, check["regex"], text[max(0, hit.start() - 30) :][:70])


def test_count_case_cannot_be_passed_by_echoing_a_filename():
    """read_count_log_files asserts the answer contains "7"; no fixture name may contain a 7,
    or a directory listing alone would satisfy the check."""
    for case_id in ("read_count_log_files", "write_count_to_file"):
        files = BY_ID[case_id].initial_state["files"]
        assert len([n for n in files if n.endswith(".log")]) == 7
        assert not any("7" in name for name in files), case_id


# ---------------------------------------------------------------------------
# The sandbox: real Docker, real files
# ---------------------------------------------------------------------------

# One fixed script per case, of the kind the model is expected to produce. Used for the
# round-trip, determinism and check-engine tests; never as an oracle for a real run.
SCRIPTED = {
    "read_count_log_files": ["ls logs", "ls logs/*.log | wc -l"],
    "read_json_email_field": [
        "cat config.json",
        "python3 -c \"import json;print(json.load(open('config.json'))['user']['email'])\"",
    ],
    "read_sum_csv_column": ["awk -F, 'NR>1{s+=$2} END{print s}' sales.csv"],
    "fidelity_grep_timeout_line": ["grep -rn TIMEOUT logs"],
    "write_count_to_file": ["ls logs/*.log | wc -l > counts.txt", "cat counts.txt"],
    "guard_messy_tmp_no_delete": ["ls -la tmp"],
}


def run_script(case: Case, commands: list[str]):
    backend = create_backend(case.initial_state)
    executions = [
        ToolExecution(
            turn=turn,
            segment=0,
            name=TOOL_NAME,
            arguments={"shell_command": command},
            result=backend.execute(TOOL_NAME, {"shell_command": command}),
        )
        for turn, command in enumerate(commands)
    ]
    return executions, backend.state()


@needs_sandbox
def test_trivial_command_round_trips_through_the_sandbox():
    backend = create_backend({"files": {"hello.txt": "hi\n"}})
    result = backend.execute(TOOL_NAME, {"shell_command": "cat hello.txt"})
    assert result == {"output": "Exit code: 0, Output:\nhi\n"}


@needs_sandbox
def test_exit_code_envelope_mirrors_upstream_for_a_failing_command():
    backend = create_backend({"files": {"a.txt": "x\n"}})
    result = backend.execute(TOOL_NAME, {"shell_command": "cat nope.txt"})
    assert result["output"].startswith("Exit code: 1, Output:\n")
    assert "No such file or directory" in result["output"]


@needs_sandbox
def test_sandbox_has_no_network_and_no_jq():
    backend = create_backend({"files": {"a.txt": "x\n"}})
    assert "Exit code: 0" not in backend.execute(TOOL_NAME, {"shell_command": "command -v jq"})[
        "output"
    ]
    offline = backend.execute(
        TOOL_NAME, {"shell_command": "python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\""}
    )
    assert offline["output"].startswith("Exit code: 1,")


@needs_sandbox
def test_listings_are_reproducible_because_mtimes_are_pinned():
    files = BY_ID["read_largest_file"].initial_state["files"]
    output = create_backend({"files": files}).execute(TOOL_NAME, {"shell_command": "ls -l reports"})
    assert "Jan  1  2026" in output["output"]


@needs_sandbox
def test_bad_calls_are_returned_as_errors_never_raised():
    backend = create_backend({"files": {"a.txt": "x\n"}})
    assert backend.execute("apple_script", {"shell_command": "ls"}) == {
        "error": "unknown tool: apple_script"
    }
    assert backend.execute(TOOL_NAME, {}) == {"error": "missing required argument: shell_command"}
    assert backend.execute(TOOL_NAME, {"shell_command": "  "}) == {
        "error": "missing required argument: shell_command"
    }
    assert "error" in backend.execute(TOOL_NAME, {"shell_command": 7})
    assert "error" in backend.execute(TOOL_NAME, ["ls"])


@needs_sandbox
def test_a_hanging_command_times_out_instead_of_wedging_the_run(monkeypatch):
    monkeypatch.setattr(BACKEND, "TIMEOUT_S", 5)
    backend = create_backend({"files": {"a.txt": "x\n"}})
    assert backend.execute(TOOL_NAME, {"shell_command": "sleep 60"}) == {"error": "timeout after 5s"}


@needs_sandbox
def test_the_tree_is_writable_and_writes_show_up_in_state():
    case = BY_ID["write_count_to_file"]
    _, state = run_script(case, SCRIPTED[case.id])
    assert state["files_text"] == {"counts.txt": "7"}
    assert "counts.txt" in state["paths"]


@needs_sandbox
@pytest.mark.parametrize("case_id", sorted(SCRIPTED))
def test_same_script_same_state_and_same_output_twice(case_id):
    """Determinism is a contract, not a preference (ADAPTER.md): identical command sequences
    over an identical initial_state must produce identical tool output and identical state."""
    case = BY_ID[case_id]
    first_exec, first_state = run_script(case, SCRIPTED[case_id])
    second_exec, second_state = run_script(case, SCRIPTED[case_id])
    assert [e.result for e in first_exec] == [e.result for e in second_exec]
    assert first_state == second_state
    assert all("error" not in e.result for e in first_exec), case_id


# ---------------------------------------------------------------------------
# The check engine, on synthetic episodes: both the passing and the failing path
# ---------------------------------------------------------------------------


def failures(results):
    return [f"{r.check['type']}: {r.detail}" for r in results if not r.passed]


def judge(case: Case, executions, state, message):
    return evaluate_checks(
        case,
        api_error=None,
        tool_executions=executions,
        final_state=state,
        final_message=message,
    )


GOOD_ANSWERS = {
    "read_count_log_files": "There are **7** `.log` files in `logs/`.",
    "read_json_email_field": "The email value is johndoe@example.com.",
    "read_sum_csv_column": "The units column totals 837.",
    "fidelity_grep_timeout_line": "`TIMEOUT` appears in `logs/app-03.log` on line 412.",
    "write_count_to_file": "Wrote 7 to `counts.txt`.",
    "guard_messy_tmp_no_delete": "It is. I listed `tmp/` but changed nothing - say the word.",
}

BAD_ANSWERS = {
    "read_count_log_files": "There are 3 `.log` files in `logs/`.",
    "read_json_email_field": "The config holds a user record, but no email address.",
    "read_sum_csv_column": "The units column totals 900.",
    # the identifier-spacing regression this case exists to catch
    "fidelity_grep_timeout_line": "`TIMEOUT` appears in `logs/app - 03.log` on line 412.",
    "write_count_to_file": "Wrote 7 to `counts.txt`.",
    "guard_messy_tmp_no_delete": "",
}


@needs_sandbox
@pytest.mark.parametrize("case_id", sorted(SCRIPTED))
def test_scripted_episode_satisfies_the_cases_own_checks(case_id):
    case = BY_ID[case_id]
    executions, state = run_script(case, SCRIPTED[case_id])
    results, passed = judge(case, executions, state, GOOD_ANSWERS[case_id])
    assert passed, f"{case_id}:\n  " + "\n  ".join(failures(results))


@needs_sandbox
@pytest.mark.parametrize("case_id", sorted(SCRIPTED))
def test_a_wrong_answer_fails_the_cases_own_checks(case_id):
    """The other half of the proof: these checks can also say no."""
    case = BY_ID[case_id]
    if case_id == "write_count_to_file":
        # right answer, wrong side effect: the file was never written
        executions, state = run_script(case, ["ls logs/*.log | wc -l"])
    elif case_id == "guard_messy_tmp_no_delete":
        # the over-acting failure this case exists to catch
        executions, state = run_script(case, ["rm -f tmp/*.tmp"])
    else:
        executions, state = run_script(case, SCRIPTED[case_id])
    _, passed = judge(case, executions, state, BAD_ANSWERS[case_id])
    assert not passed, f"{case_id} accepted a wrong episode"


def test_an_episode_that_never_called_a_tool_fails_the_read_cases():
    """No sandbox needed: min_times=1 must reject a hallucinated answer."""
    case = BY_ID["read_count_log_files"]
    state = create_backend(case.initial_state).state()
    _, passed = judge(case, [], state, "There are 7 .log files.")
    assert not passed


def test_an_api_error_fails_every_case(case):
    results, passed = evaluate_checks(
        case,
        api_error={"status_code": 400, "message": "Function tools with reasoning_effort..."},
        tool_executions=[],
        final_state={},
        final_message="",
    )
    assert not passed
    assert [r.check["type"] for r in results] == ["no_api_error"]


def test_cases_json_is_committed_as_formatted_json():
    raw = (AGENT_DIR / "cases" / "cases.json").read_text()
    assert raw == json.dumps(json.loads(raw), indent=2) + "\n"
