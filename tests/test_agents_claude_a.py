"""Mechanical validation of the two Claude proof-target agent directories.

No API key is read and no API call is made anywhere in this file — every run below goes through
the local `sim` provider. What is proved here:

1. Both directories satisfy the ADAPTER.md contract: `validate_agent_dir` accepts them and
   `runner.load_backend_factory` loads their backends.
2. The extracted prompts and tool schemas are byte-identical to their upstream sources
   (FACT's `Config.system_prompt` literal; the cookbook notebook's cell-33 literals).
3. The backends are deterministic (same calls twice -> same results and same state), never
   raise, and — for FACT — cannot write to the database by any route.
4. Every check in every case evaluates cleanly against a synthetic episode: no unknown check
   type, no "check could not be evaluated".
5. `run_suite` on `sim-fable-5` passes every case, and on `sim-fable-5-1` every case fails with
   the documented forced-`tool_choice` 400.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upshift import cli
from upshift.checks import evaluate_checks
from upshift.providers.sim import FORCED_TOOL_CHOICE_MESSAGE, SimProvider
from upshift.runner import load_backend_factory, run_suite
from upshift.schemas import AgentConfig, Case, ToolExecution

FACT_DIR = ROOT / "agents" / "fact"
SMS_DIR = ROOT / "agents" / "cookbook-sms"
AGENT_DIRS = {"fact": FACT_DIR, "cookbook-sms": SMS_DIR}

EXPECTED_CASE_COUNT = {"fact": 7, "cookbook-sms": 5}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FACT_BACKEND = _load_module("fact_backend", FACT_DIR / "backend.py")
SMS_BACKEND = _load_module("sms_backend", SMS_DIR / "backend.py")
BACKENDS = {"fact": FACT_BACKEND, "cookbook-sms": SMS_BACKEND}

CASES = {name: Case.load_all(d / "cases" / "cases.json") for name, d in AGENT_DIRS.items()}


# ---------------------------------------------------------------------------
# 1. The ADAPTER.md contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_validate_agent_dir_accepts(name):
    raw = cli.validate_agent_dir(AGENT_DIRS[name])
    assert raw["endpoint"] == "messages"
    assert raw["model"] == "claude-fable-5"
    # The break under test must still be in the config: without it there is nothing to detect.
    assert raw["params"]["tool_choice"] == {"type": "any"}
    assert raw["params"]["max_tokens"] > 0
    assert "timeout" not in raw["params"], "timeout is transport, not a request-body param"


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_runner_loads_the_backend(name):
    factory = load_backend_factory(AGENT_DIRS[name])
    backend = factory({})
    assert isinstance(backend.state(), dict)


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_case_count_and_unique_ids(name):
    cases = CASES[name]
    assert len(cases) == EXPECTED_CASE_COUNT[name]
    assert len({c.id for c in cases}) == len(cases)
    for case in cases:
        assert case.description.strip()
        assert case.user_messages
        assert case.sim.get("oracle_plan"), f"{case.id} needs a sim.oracle_plan"


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_tools_are_chat_style_and_named_by_the_cases(name):
    config = AgentConfig.load(AGENT_DIRS[name])
    names = set()
    for tool in config.tools:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert fn["description"].strip()
        assert fn["parameters"]["type"] == "object"
        names.add(fn["name"])
    for case in CASES[name]:
        for check in case.checks:
            if check["type"] in ("tool_called", "tool_not_called"):
                assert check["name"] in names, f"{case.id} names an unknown tool"
        for step in case.sim["oracle_plan"]:
            for call in step.get("tool_calls", []):
                assert call["name"] in names


# ---------------------------------------------------------------------------
# 2. Verbatim extraction from upstream
# ---------------------------------------------------------------------------

# `src/core/config.py:141-162` at ruvnet/FACT b0e3435 — the default of Config.system_prompt.
FACT_PROMPT_HEAD = (
    "You are a finance assistant with access to SQL database tools. You MUST use tools to "
    "answer questions about financial data."
)
FACT_PROMPT_TAIL = "Always show real data, not placeholders or descriptions of what you would do."

# `tool_use/tool_choice.ipynb` cell 33 at anthropics/claude-cookbooks bbfab1b.
SMS_PROMPT = (
    "\n"
    "All your communication with a user is done via text message.\n"
    "Only call tools when you have enough information to accurately call them.\n"
    "Do not call the get_customer_info tool until a user has provided you with their username. "
    "This is important.\n"
    "If you do not know a user's username, simply ask a user for their username.\n"
)


def test_fact_prompt_is_the_upstream_literal():
    prompt = (FACT_DIR / "system_prompt.txt").read_text()
    assert prompt.startswith(FACT_PROMPT_HEAD)
    # No trailing newline: the upstream literal ends on the period.
    assert prompt.endswith(FACT_PROMPT_TAIL)
    assert len(prompt) == 877


def test_sms_prompt_is_the_notebook_literal():
    assert (SMS_DIR / "system_prompt.txt").read_text() == SMS_PROMPT


def test_sms_tools_match_the_notebook_cell():
    tools = json.loads((SMS_DIR / "tools.json").read_text())
    by_name = {t["function"]["name"]: t["function"] for t in tools}
    assert set(by_name) == {"send_text_to_user", "get_customer_info"}
    send = by_name["send_text_to_user"]
    assert send["description"] == "Sends a text message to a user"
    assert send["parameters"] == {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The piece of text to be sent to the user via text message",
            }
        },
        "required": ["text"],
    }
    info = by_name["get_customer_info"]
    # Double space and trailing space are the notebook's; they must survive verbatim.
    assert info["description"] == (
        "gets information on a customer based on the customer's username.  Response includes "
        "email, username, and previous purchases. Only call this tool once a user has provided "
        "you with their username"
    )
    assert info["parameters"]["properties"]["username"]["description"] == (
        "The username of the user in question. "
    )


def test_fact_tools_match_the_upstream_decorators():
    tools = json.loads((FACT_DIR / "tools.json").read_text())
    by_name = {t["function"]["name"]: t["function"] for t in tools}
    assert set(by_name) == {"SQL_QueryReadonly", "SQL_GetSchema", "SQL_GetSampleQueries"}
    statement = by_name["SQL_QueryReadonly"]["parameters"]["properties"]["statement"]
    assert statement["minLength"] == 10
    assert statement["maxLength"] == 1000
    assert by_name["SQL_QueryReadonly"]["parameters"]["required"] == ["statement"]
    for empty in ("SQL_GetSchema", "SQL_GetSampleQueries"):
        assert by_name[empty]["parameters"]["properties"] == {}
        assert by_name[empty]["parameters"]["required"] == []


def test_fact_sample_queries_are_upstream_verbatim():
    """The seven-entry list at src/tools/connectors/sql.py:291-323."""
    assert len(FACT_BACKEND.SAMPLE_QUERIES) == 7
    descriptions = [q["description"] for q in FACT_BACKEND.SAMPLE_QUERIES]
    assert descriptions == [
        "Get all companies in the Technology sector",
        "Get total revenue by company for 2024",
        "Get Q1 2025 financial results",
        "Get company count by sector",
        "Get TechCorp's quarterly performance over time",
        "Get average metrics for 2024",
        "Get top companies by market cap with latest revenue",
    ]


def test_sms_customer_record_is_the_notebook_mock():
    assert SMS_BACKEND.get_customer_info("jenny76") == {
        "username": "jenny76",
        "email": "jenny76@email.com",
        "purchases": [
            {"id": 1, "product": "computer mouse"},
            {"id": 2, "product": "screen protector"},
            {"id": 3, "product": "usb charging cable"},
        ],
    }


# ---------------------------------------------------------------------------
# 3. Backend behaviour: determinism, never-raises, read-only
# ---------------------------------------------------------------------------

FACT_CALLS = [
    ("SQL_GetSchema", {}),
    ("SQL_GetSampleQueries", {}),
    ("SQL_QueryReadonly", {"statement": "SELECT name FROM companies WHERE sector = 'Technology'"}),
    ("SQL_QueryReadonly", {"statement": "SELECT SUM(employees) AS e FROM companies"}),
    ("SQL_QueryReadonly", {"statement": "DELETE FROM benchmarks"}),
]

SMS_CALLS = [
    ("get_customer_info", {"username": "jenny76"}),
    ("send_text_to_user", {"text": "your email is jenny76@email.com"}),
    ("get_customer_info", {"username": "marco_p"}),
]

DETERMINISM_CASES = {"fact": FACT_CALLS, "cookbook-sms": SMS_CALLS}


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_backend_is_deterministic(name):
    """Same initial_state + same call sequence -> same results and same final state."""

    def run():
        backend = BACKENDS[name].create_backend({})
        results = [backend.execute(tool, args) for tool, args in DETERMINISM_CASES[name]]
        return results, backend.state()

    first_results, first_state = run()
    second_results, second_state = run()
    assert first_results == second_results
    assert first_state == second_state
    # And JSON-serializable, because the runner records it.
    json.dumps(first_state)
    json.dumps(first_results)


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_backend_never_raises(name):
    backend = BACKENDS[name].create_backend({})
    hostile = [
        ("no_such_tool", {}),
        ("SQL_QueryReadonly", {}),
        ("SQL_QueryReadonly", {"statement": None}),
        ("SQL_QueryReadonly", {"statement": "SELECT * FROM nope"}),
        ("SQL_QueryReadonly", {"statement": "SELECT ("}),
        ("send_text_to_user", {}),
        ("send_text_to_user", {"text": ""}),
        ("get_customer_info", {}),
        ("get_customer_info", {"username": 17}),
    ]
    for tool, args in hostile:
        result = backend.execute(tool, args)
        assert isinstance(result, dict)
        json.dumps(result)
    for bad_args in (None, [], "statement", 3):
        result = backend.execute("SQL_QueryReadonly", bad_args)
        assert isinstance(result, dict) and "error" in result
    assert isinstance(backend.state(), dict)


WRITE_STATEMENTS = [
    "DELETE FROM benchmarks",
    "delete from benchmarks where id > 0",
    "UPDATE companies SET employees = 1",
    "INSERT INTO companies (name, symbol, sector) VALUES ('X', 'X', 'X')",
    "DROP TABLE benchmarks",
    "CREATE TABLE evil (id INTEGER)",
    "ATTACH DATABASE 'other.db' AS other",
    "PRAGMA writable_schema = 1",
    "VACUUM",
    "SELECT 1; DROP TABLE companies",
]


@pytest.mark.parametrize("statement", WRITE_STATEMENTS)
def test_fact_rejects_every_write(statement):
    backend = FACT_BACKEND.create_backend({})
    result = backend.execute("SQL_QueryReadonly", {"statement": statement})
    assert result["status"] == "failed"
    assert result["error"]
    state = backend.state()
    assert state["statements"] == [], "a rejected statement must not count as executed"
    assert statement in state["rejected_statements"]


def test_fact_database_handle_is_read_only():
    """Even bypassing validate_sql_query, the handle itself cannot write."""
    conn = FACT_BACKEND._open_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM benchmarks")
    finally:
        conn.close()


def test_fact_database_file_is_unchanged_by_a_run():
    before = FACT_BACKEND.DB_PATH.read_bytes()
    backend = FACT_BACKEND.create_backend({})
    for tool, args in FACT_CALLS:
        backend.execute(tool, args)
    assert FACT_BACKEND.DB_PATH.read_bytes() == before


def test_fact_results_mirror_the_upstream_envelope():
    backend = FACT_BACKEND.create_backend({})
    ok = backend.execute(
        "SQL_QueryReadonly", {"statement": "SELECT name FROM companies ORDER BY id LIMIT 1"}
    )
    assert set(ok) == {
        "query_id", "rows", "row_count", "columns", "execution_time_ms", "statement", "status"
    }
    assert ok["status"] == "success"
    assert ok["rows"] == [{"name": "TechCorp Inc."}]
    assert ok["columns"] == ["name"]
    # Clock-derived fields are deterministic stand-ins (ATTRIBUTION, "Deltas").
    assert ok["execution_time_ms"] == 0.0
    assert ok["query_id"] == "query_1"

    schema = backend.execute("SQL_GetSchema", {})
    assert schema["database_type"] == "SQLite"
    assert [t["name"] for t in schema["tables"]] == [
        "benchmarks", "companies", "financial_data", "financial_records"
    ]


def test_sms_delivered_projection():
    backend = SMS_BACKEND.create_backend({})
    assert backend.state()["delivered"] == {
        "username": False, "email": False, "purchases": False
    }
    backend.execute("send_text_to_user", {"text": "jenny76@email.com"})
    # Nothing was looked up, so nothing can be "delivered".
    assert backend.state()["delivered"]["email"] is False

    backend = SMS_BACKEND.create_backend({})
    backend.execute("get_customer_info", {"username": "jenny76"})
    assert backend.state()["delivered"]["email"] is False
    backend.execute("send_text_to_user", {"text": "We have JENNY76@EMAIL.COM on file."})
    delivered = backend.state()["delivered"]
    assert delivered["email"] is True
    assert delivered["username"] is True
    assert delivered["purchases"] is False


# ---------------------------------------------------------------------------
# 4. Every check evaluates cleanly
# ---------------------------------------------------------------------------


def _synthetic_episode(name: str, case: Case):
    """Replay the case's own oracle plan through the real backend, without any provider.

    This is not a pass/fail assertion — it exists so every check runs against a realistic
    episode shape and can be inspected for evaluation errors.
    """
    backend = BACKENDS[name].create_backend(case.initial_state)
    executions: list[ToolExecution] = []
    turn = 0
    final_message = ""
    segment = 0
    for step in case.sim["oracle_plan"]:
        calls = step.get("tool_calls") or []
        if calls:
            for call in calls:
                args = {
                    k: v for k, v in call.get("arguments", {}).items()
                    if not (isinstance(v, str) and v.startswith("$ref:"))
                }
                executions.append(
                    ToolExecution(
                        turn=turn,
                        segment=segment,
                        name=call["name"],
                        arguments=call.get("arguments", {}),
                        result=backend.execute(call["name"], args),
                    )
                )
            turn += 1
        if "final_message" in step:
            final_message = step["final_message"]
            segment += 1
    return executions, backend.state(), final_message


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_every_check_evaluates_without_error(name):
    bad: list[str] = []
    for case in CASES[name]:
        executions, state, message = _synthetic_episode(name, case)
        results, _ = evaluate_checks(
            case,
            api_error=None,
            tool_executions=executions,
            final_state=state,
            final_message=message,
        )
        assert len(results) == len(case.checks)
        for result in results:
            if "could not be evaluated" in result.detail or "unknown check type" in result.detail:
                bad.append(f"{case.id}: {result.detail}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_an_errored_episode_fails_every_case(name):
    """The hard rule in checks.py: an API error is one failed no_api_error and nothing else."""
    for case in CASES[name]:
        results, passed = evaluate_checks(
            case,
            api_error={"status_code": 400, "message": FORCED_TOOL_CHOICE_MESSAGE},
            tool_executions=[],
            final_state={},
            final_message="",
        )
        assert not passed
        assert len(results) == 1
        assert results[0].check["type"] == "no_api_error"


def test_fact_checks_do_not_leak_their_answers():
    """Anti-leak discipline: an asserted value must not be readable off the agent's own files."""
    config = AgentConfig.load(FACT_DIR)
    haystack = (config.system_prompt + json.dumps(config.tools)).lower()
    for case in CASES["fact"]:
        question = " ".join(case.user_messages).lower()
        for check in case.checks:
            if check["type"] == "response_contains":
                text = check["text"].lower()
                assert text not in haystack, f"{case.id}: '{text}' is in the agent's own files"
                assert text not in question, f"{case.id}: '{text}' is in the question"
            if check["type"] == "response_matches" and check["regex"].startswith("\\b"):
                assert not re.search(check["regex"], haystack)
                assert not re.search(check["regex"], question)


# ---------------------------------------------------------------------------
# 5. The sim pipeline, end to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_sim_fable_5_passes_every_case(tmp_path, name):
    run_dir = run_suite(
        AGENT_DIRS[name],
        SimProvider(),
        f"test-{name}-fable5",
        n_reps=1,
        model_override="sim-fable-5",
        runs_root=tmp_path,
        workers=1,
    )
    summary = json.loads((run_dir / "summary.json").read_text())
    assert len(summary) == EXPECTED_CASE_COUNT[name]
    failed = {cid: s for cid, s in summary.items() if s["passes"] != s["n"]}
    if failed:
        details = []
        for cid in failed:
            for rep in sorted((run_dir / "cases" / cid).glob("rep_*.json")):
                record = json.loads(rep.read_text())
                details += [
                    f"{cid}: {c['detail']}" for c in record["check_results"] if not c["passed"]
                ]
        pytest.fail("\n".join(details))


def test_sms_makes_exactly_one_api_call_per_user_message(tmp_path):
    """Harness fidelity with the notebook: `sms_chatbot` calls messages.create ONCE per
    incoming message and executes the returned tool call — the tool call IS the reply. It
    never sends tool results back. max_turns counts API calls per EPISODE, so max_turns: 1
    reproduces that exactly: one request, tools executed, no follow-up."""
    config = AgentConfig.load(SMS_DIR)
    assert config.max_turns == 1

    run_dir = run_suite(
        SMS_DIR,
        SimProvider(),
        "test-sms-one-call",
        n_reps=1,
        model_override="sim-fable-5",
        runs_root=tmp_path,
        workers=1,
    )
    for case in CASES["cookbook-sms"]:
        # one message in, one message out: no case can need a second call
        assert len(case.user_messages) == 1, case.id
        reps = sorted((run_dir / "cases" / case.id).glob("rep_*.json"))
        assert len(reps) == 1, case.id
        record = json.loads(reps[0].read_text())
        assert len(record["api_calls"]) == 1, case.id
        # the single call did produce a tool execution (tool_choice: any is in force)
        assert len(record["tool_executions"]) >= 1, case.id
        assert all(e["turn"] == 0 for e in record["tool_executions"]), case.id
        # and nothing was ever sent back: the request carries only the user's own message
        assert record["api_calls"][0]["request"]["messages"] == [
            {"role": "user", "content": case.user_messages[0]}
        ], case.id


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_sim_fable_5_1_400s_every_case_on_the_forced_tool_choice(tmp_path, name):
    run_dir = run_suite(
        AGENT_DIRS[name],
        SimProvider(),
        f"test-{name}-fable51",
        n_reps=1,
        model_override="sim-fable-5-1",
        runs_root=tmp_path,
        workers=1,
    )
    summary = json.loads((run_dir / "summary.json").read_text())
    assert len(summary) == EXPECTED_CASE_COUNT[name]
    assert all(s["passes"] == 0 for s in summary.values()), summary
    for cid in summary:
        for rep in sorted((run_dir / "cases" / cid).glob("rep_*.json")):
            record = json.loads(rep.read_text())
            assert record["api_error"]["status_code"] == 400
            assert FORCED_TOOL_CHOICE_MESSAGE in record["api_error"]["message"]
            # The 400 is the only thing reported; no behavioral check is evaluated.
            assert len(record["check_results"]) == 1
            assert record["check_results"][0]["check"]["type"] == "no_api_error"
