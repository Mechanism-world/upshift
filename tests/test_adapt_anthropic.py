"""`upshift adapt` on an agent built for the Anthropic Messages API (DESIGN.md v0.3).

The whole file is offline and free: the extraction engine takes an injectable `call_model`,
so a scripted reply stands in for the provider and nothing here touches a network.

What it pins down, end to end on `tests/adapt_fixtures/anthropic_handrolled`:

* the static inventory ranks an Anthropic repo the way it ranks an OpenAI one, and the AST
  captures the `messages.create` kwargs (model, max_tokens, system, tools, tool_choice,
  output_config);
* the extraction schema accepts `endpoint: "messages"` and object-valued `tool_choice` /
  `thinking`, and rejects an object anywhere else in `params`;
* the generated directory is canonical upshift, not Anthropic-shaped: `input_schema` becomes
  chat-style `parameters`, `output_config.effort` becomes `reasoning_effort`, `max_tokens`
  and the Anthropic `tool_choice` object survive as parameters;
* a retrieval-shaped tool's `tool_called` check carries `retrieval: true` for the differ;
* the verification gate accepts a `messages.create` marker for "messages" and refuses to
  accept it for "chat_completions".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift import cli
from upshift.adapt.extract import DICT_PARAMS, ENDPOINT_VALUES, extract, validate_extraction
from upshift.adapt.generate import build_params, generate
from upshift.adapt.inventory import resolve_source, take_inventory
from upshift.adapt.report import CostInfo, render_report
from upshift.adapt.verify import verify
from upshift.providers.sim import SimProvider
from upshift.recorder import load_case_reps
from upshift.runner import run_suite
from upshift.schemas import ENDPOINTS, AgentConfig, Case

ROOT = Path(__file__).resolve().parents[1]
ANTHROPIC = ROOT / "tests" / "adapt_fixtures" / "anthropic_handrolled"
HANDROLLED = ROOT / "tests" / "adapt_fixtures" / "handrolled"


def inventory_of(path: Path, tmp_path: Path, **kwargs):
    repo = resolve_source(str(path), tmp_path)
    return repo, take_inventory(repo, **kwargs)


# ---------------------------------------------------------------------------
# The scripted extraction: what a good model reply looks like for this fixture
# ---------------------------------------------------------------------------

EXTRACTION = {
    "agent_name": "docly",
    "endpoint": {"value": "messages", "citation": "docs_agent.py:63", "status": "found",
                 "note": "client.messages.create — the Anthropic Messages API"},
    "model": {"value": "claude-fable-5", "citation": "docs_agent.py:64", "status": "found",
              "note": ""},
    "params": {
        "value": {
            "max_tokens": 2048,
            "tool_choice": {"type": "tool", "name": "search_docs"},
            "reasoning_effort": "low",
        },
        "citation": "docs_agent.py:65",
        "status": "found",
        "note": "reasoning_effort is output_config={'effort': 'low'} at docs_agent.py:70",
    },
    "max_turns": {"value": None, "citation": "", "status": "undetermined",
                  "note": "the while loop is unbounded (docs_agent.py:62)"},
    "system_prompt": {
        "status": "found",
        "note": "system= is a plain string built from three adjacent literals",
        "chunks": [
            {"text": "You are Docly, a documentation assistant for the Ledger handbook.",
             "kind": "verbatim", "citation": "docs_agent.py:13", "note": ""},
            {"text": "Search the handbook before you answer, and never invent a section "
                     "number.",
             "kind": "verbatim", "citation": "docs_agent.py:14", "note": ""},
            {"text": "Answer in at most three sentences.", "kind": "verbatim",
             "citation": "docs_agent.py:15", "note": ""},
        ],
    },
    "tools": [
        {
            "name": "search_docs",
            "description": "Search the handbook and return matching sections.",
            # Anthropic `input_schema`, reported under the canonical key.
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string",
                                         "description": "What to search for."}},
                "required": ["query"],
            },
            "kind": "verbatim",
            "citation": "docs_agent.py:20",
            "backend": {"kind": "lookup", "state_key": "sections", "id_field": "",
                        "id_prefix": "", "match_fields": ["query"], "text_field": "",
                        "citation": "docs_agent.py:49",
                        "reason": "pure filter over the SECTIONS index"},
        },
        {
            "name": "open_ticket",
            "description": "Open a support ticket when the handbook does not answer the "
                           "question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One-line summary."},
                    "detail": {"type": "string",
                               "description": "Everything the user told us."},
                },
                "required": ["summary"],
            },
            "kind": "verbatim",
            "citation": "docs_agent.py:29",
            "backend": {"kind": "unclear", "state_key": "", "id_field": "", "id_prefix": "",
                        "match_fields": [], "text_field": "", "citation": "docs_agent.py:55",
                        "reason": "talks to the real ticketing service"},
        },
    ],
    "cases": [
        {
            "id": "wifi_lookup",
            "description": "README usage example.",
            "user_messages": ["What is the office wifi password?"],
            "initial_state": {"sections": [
                {"query": "office wifi", "section_id": "S-3", "body": "The password is "
                                                                     "hunter2."},
            ]},
            "expected_tool_calls": [
                {"name": "search_docs", "arguments": {"query": "office wifi"}},
            ],
            "final_message": "Section S-3 says the password is hunter2.",
            "checks": [{"type": "response_contains", "text": "hunter2"}],
            "citation": "README.md:9",
            "note": "",
        },
    ],
    "undetermined": [
        {"what": "max_turns", "pointer": "docs_agent.py:62",
         "why": "the while loop has no bound"},
    ],
    "notes": "",
}


def chat_response(content: str) -> dict:
    return {
        "id": "chatcmpl-fake",
        "model": "gpt-5.5-2026-01-01",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    }


def scripted(*replies: str):
    seen: list[dict] = []

    def call_model(request: dict) -> dict:
        seen.append(request)
        return chat_response(replies[min(len(seen), len(replies)) - 1])

    call_model.requests = seen  # type: ignore[attr-defined]
    return call_model


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """The whole pipeline over the Anthropic fixture, with the model scripted."""
    tmp = tmp_path_factory.mktemp("adapt-anthropic")
    repo = resolve_source(str(ANTHROPIC), tmp)
    inventory = take_inventory(repo)
    extraction = extract(
        inventory, call_model=scripted(json.dumps(EXTRACTION)), model="gpt-5.5",
        second_round=False,
    )
    assert extraction.ok, extraction.errors
    verification = verify(extraction.data, repo.root)
    out_dir = tmp / "docly_agent"
    result = generate(verification, out_dir, origin=str(ANTHROPIC), commit="cafe1234")
    return verification, result, out_dir


# ---------------------------------------------------------------------------
# Stage 1: the Anthropic SDK as a static signal
# ---------------------------------------------------------------------------


def test_the_anthropic_agent_file_ranks_first(tmp_path):
    _, inv = inventory_of(ANTHROPIC, tmp_path)
    assert inv.files[0].path == "docs_agent.py"
    # Same rung as the OpenAI fixture: an Anthropic repo must not rank lower for being
    # Anthropic.
    _, openai_inv = inventory_of(HANDROLLED, tmp_path / "openai")
    assert inv.files[0].score >= openai_inv.files[0].score * 0.8
    reasons = " ".join(inv.files[0].reasons)
    for expected in ("messages.create call", "anthropic import",
                     "Anthropic client construction", "Anthropic tool schema literal",
                     "output_config"):
        assert expected in reasons, expected


def test_the_ast_captures_the_messages_create_kwargs(tmp_path):
    _, inv = inventory_of(ANTHROPIC, tmp_path)
    sites = [s for s in inv.call_sites if s.callee.endswith("messages.create")]
    assert len(sites) == 1
    site = sites[0]
    assert (site.how, site.path) == ("ast", "docs_agent.py")
    assert site.kwargs["model"] == "'claude-fable-5'"
    assert site.kwargs["max_tokens"] == "2048"
    assert site.kwargs["system"] == "SYSTEM_PROMPT"
    assert site.kwargs["tools"] == "TOOLS"
    assert site.kwargs["tool_choice"] == "{'type': 'tool', 'name': 'search_docs'}"
    assert site.kwargs["output_config"] == "{'effort': 'low'}"
    line = (ANTHROPIC / "docs_agent.py").read_text().splitlines()[site.line - 1]
    assert "messages.create" in line


def test_a_typescript_anthropic_agent_is_still_seen(tmp_path):
    """No AST for .ts — the regexes and the regex call-site fallback carry it."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "agent.ts").write_text(
        'import Anthropic from "@anthropic-ai/sdk";\n'
        'const client = new Anthropic();\n'
        'const SYSTEM = "You are a helpful assistant.";\n'
        'const res = await client.messages.create({model: "claude-fable-5", '
        'max_tokens: 1024, system: SYSTEM, tools: TOOLS});\n'
    )
    _, inv = inventory_of(repo_dir, tmp_path / "work")
    assert [f.path for f in inv.files] == ["agent.ts"]
    reasons = " ".join(inv.files[0].reasons)
    assert "@anthropic-ai/sdk import" in reasons
    site = next(s for s in inv.call_sites if "messages.create" in s.callee)
    assert (site.how, site.line) == ("regex", 4)


# ---------------------------------------------------------------------------
# Stage 2: the extraction schema
# ---------------------------------------------------------------------------


def test_messages_is_a_legal_endpoint_value():
    assert "messages" in ENDPOINT_VALUES
    assert validate_extraction(EXTRACTION) == []


def test_the_prompt_tells_the_model_how_to_read_an_anthropic_call_site(tmp_path):
    from upshift.adapt.extract import build_messages

    _, inv = inventory_of(ANTHROPIC, tmp_path)
    user = build_messages(inv)[1]["content"]
    for expected in ("client.messages.create", 'is "messages"', "output_config",
                     "params.reasoning_effort", "input_schema", "list of text blocks"):
        assert expected in user, expected


@pytest.mark.parametrize("key", DICT_PARAMS)
def test_tool_choice_and_thinking_may_be_objects(key):
    data = json.loads(json.dumps(EXTRACTION))
    data["params"]["value"] = {key: {"type": "enabled", "budget_tokens": 4000}}
    assert validate_extraction(data) == []


def test_every_other_parameter_stays_scalar():
    data = json.loads(json.dumps(EXTRACTION))
    data["params"]["value"] = {"output_config": {"effort": "low"}}
    errors = validate_extraction(data)
    assert any("params.value.output_config" in e for e in errors), errors
    assert any("only tool_choice/thinking may be an object" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Stage 3: the gate
# ---------------------------------------------------------------------------


def test_the_gate_accepts_a_messages_create_marker():
    verification = verify(EXTRACTION, ANTHROPIC)
    endpoint = verification.data["endpoint"]
    assert endpoint["verified"] is True
    assert "messages.create" in endpoint["verify_detail"]


def test_a_messages_create_call_site_never_confirms_chat_completions():
    """The one direction that would be a silent disaster: reading an Anthropic call site as
    an OpenAI one, and then reporting the gpt-5.6 chat+tools break for an agent that never
    goes near /v1/chat/completions."""
    data = json.loads(json.dumps(EXTRACTION))
    data["endpoint"]["value"] = "chat_completions"
    verification = verify(data, ANTHROPIC)
    assert verification.data["endpoint"]["verified"] is False
    assert any(f.what == "endpoint" for f in verification.flags_for("agent.json"))


def test_the_canonical_effort_parameter_is_grounded_on_its_wire_spelling():
    """`reasoning_effort` is upshift's name for `output_config.effort`; the gate looks for
    the spelling the source actually uses rather than dropping a real parameter."""
    verification = verify(EXTRACTION, ANTHROPIC)
    assert verification.dropped_params == []
    assert verification.data["params"]["value"] == EXTRACTION["params"]["value"]


def test_an_effort_claim_with_no_output_config_in_sight_is_still_dropped():
    data = json.loads(json.dumps(EXTRACTION))
    data["params"]["citation"] = "README.md:9"  # no output_config, no effort, no tool_choice
    verification = verify(data, ANTHROPIC)
    assert "reasoning_effort" in verification.dropped_params
    assert "max_tokens" in verification.dropped_params


# ---------------------------------------------------------------------------
# Stage 4: the generated directory is canonical upshift, not Anthropic-shaped
# ---------------------------------------------------------------------------


def test_agent_json_is_canonical(generated):
    _, _, out_dir = generated
    config = json.loads((out_dir / "agent.json").read_text())
    assert config["endpoint"] == "messages"
    assert config["model"] == "claude-fable-5"
    assert config["params"] == {
        "max_tokens": 2048,
        "tool_choice": {"type": "tool", "name": "search_docs"},
        "reasoning_effort": "low",
    }
    assert config["max_turns"] == 12  # undetermined -> the documented default


def test_tools_json_is_chat_style_with_input_schema_as_parameters(generated):
    _, _, out_dir = generated
    tools = json.loads((out_dir / "tools.json").read_text())
    assert [t["function"]["name"] for t in tools] == ["search_docs", "open_ticket"]
    for tool in tools:
        assert set(tool) == {"type", "function"}
        assert set(tool["function"]) == {"name", "description", "parameters"}
        assert "input_schema" not in json.dumps(tool)
    assert tools[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to search for."}},
        "required": ["query"],
    }


def test_an_extraction_that_reports_the_wire_names_is_canonicalised(tmp_path):
    """A model that answers in Anthropic's own vocabulary must not produce an agent.json
    nothing downstream understands."""
    data = json.loads(json.dumps(EXTRACTION))
    data["params"]["value"] = {"max_tokens": 2048, "output_config": {"effort": "high"}}
    data["tools"][0].pop("parameters")
    data["tools"][0]["input_schema"] = {"type": "object", "properties": {},
                                        "required": []}
    verification = verify(data, ANTHROPIC)
    result = generate(verification, tmp_path / "wire", origin="fixture", commit=None)
    config = json.loads((tmp_path / "wire" / "agent.json").read_text())
    assert config["params"] == {"max_tokens": 2048, "reasoning_effort": "high"}
    assert any("output_config.effort" in note for note in result.notes)
    tools = json.loads((tmp_path / "wire" / "tools.json").read_text())
    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {},
                                                 "required": []}


def test_the_system_prompt_is_the_verified_source_lines(generated):
    _, _, out_dir = generated
    assert (out_dir / "system_prompt.txt").read_text() == (
        "You are Docly, a documentation assistant for the Ledger handbook.\n"
        "Search the handbook before you answer, and never invent a section number.\n"
        "Answer in at most three sentences.\n"
    )


def test_the_system_prompt_is_never_a_request_parameter():
    notes: list[str] = []
    params = build_params(
        {"system": "You are Docly.", "max_tokens": 512}, "messages", notes
    )
    assert params == {"max_tokens": 512}
    assert any("'system'" in note for note in notes)


def test_a_retrieval_shaped_tool_check_carries_the_differ_flag(generated):
    _, _, out_dir = generated
    case = json.loads((out_dir / "cases" / "cases.json").read_text())[0]
    called = {c["name"]: c for c in case["checks"] if c["type"] == "tool_called"}
    assert called["search_docs"]["retrieval"] is True
    # Inert for pass/fail: the check engine has no opinion about the key.
    from upshift.checks import evaluate_checks
    from upshift.schemas import ToolExecution

    results, passed = evaluate_checks(
        Case.load_all(out_dir / "cases" / "cases.json")[0],
        api_error=None,
        tool_executions=[ToolExecution(name="search_docs",
                                       arguments={"query": "office wifi"},
                                       result={"results": []}, turn=1, segment=0)],
        final_state={},
        final_message="Section S-3 says the password is hunter2.",
    )
    assert passed, [(r.check, r.detail) for r in results if not r.passed]


def test_the_report_names_the_anthropic_api_and_the_right_migration(generated, tmp_path):
    verification, result, out_dir = generated
    repo = resolve_source(str(ANTHROPIC), tmp_path)
    text = render_report(
        inventory=take_inventory(repo), extraction=None, verification=verification,
        generation=result,
        cost=CostInfo("openai", "gpt-5.5", 9_000, 800, 0, 0.069, "/runs/adapt-docly",
                      "adapt-docly"),
        out_dir=out_dir,
    )
    assert "messages (Anthropic Messages API)" in text
    assert "--baseline-model sim-fable-5 --candidate-model sim-fable-5-1" in text
    assert "--provider anthropic" in text
    assert "gpt-5.6-sol" not in text


# ---------------------------------------------------------------------------
# The generated directory as a real agent directory
# ---------------------------------------------------------------------------


def test_the_generated_files_are_all_there(generated):
    _, result, _out_dir = generated
    assert set(result.files) == {
        "agent.json", "system_prompt.txt", "tools.json", "backend.py", "cases/cases.json",
        "PROVENANCE.json",
    }
    assert result.implemented_tools == ["search_docs"]
    assert result.stub_tools == ["open_ticket"]


def test_the_generated_directory_satisfies_the_adapter_contract(generated):
    assert "messages" in ENDPOINTS, "the agent loop must register the endpoint upshift writes"
    _, _, out_dir = generated
    config = cli.validate_agent_dir(out_dir)
    assert config["endpoint"] == "messages"


def test_the_generated_directory_round_trips_through_agent_config(generated):
    _, _, out_dir = generated
    config = AgentConfig.load(out_dir)
    assert config.endpoint == "messages"
    assert config.model == "claude-fable-5"
    assert config.params["tool_choice"] == {"type": "tool", "name": "search_docs"}
    assert config.params["max_tokens"] == 2048
    assert config.params["reasoning_effort"] == "low"
    assert [t["function"]["name"] for t in config.tools] == ["search_docs", "open_ticket"]
    assert config.system_prompt.startswith("You are Docly,")


def test_the_generated_agent_runs_on_the_fable_sim(generated, tmp_path):
    """The point of the whole exercise: the directory adapt writes is an ordinary upshift
    agent dir, so `upshift upgrade --provider sim` runs it with no special-casing."""
    _, _, out_dir = generated
    runs_root = tmp_path / "runs"
    run_suite(
        out_dir, SimProvider(), "docly-baseline", n_reps=2, model_override="sim-fable-5",
        runs_root=runs_root, workers=1,
    )
    reps = load_case_reps(runs_root / "docly-baseline", "wifi_lookup")
    assert len(reps) == 2
    assert all(r.passed for r in reps), [
        (c.check, c.detail) for r in reps for c in r.check_results if not c.passed
    ]
    assert [t.name for t in reps[0].tool_executions] == ["search_docs"]


def test_the_documented_forced_tool_choice_break_fires_on_the_generated_agent(
    generated, tmp_path
):
    """`tool_choice={"type": "tool", ...}` is exactly what claude-fable-5-1 rejects, and it
    survived extraction into agent.json — so the generated agent reproduces the break the
    fixture was chosen for."""
    _, _, out_dir = generated
    runs_root = tmp_path / "runs"
    run_suite(
        out_dir, SimProvider(), "docly-candidate", n_reps=1,
        model_override="sim-fable-5-1", runs_root=runs_root, workers=1,
    )
    rep = load_case_reps(runs_root / "docly-candidate", "wifi_lookup")[0]
    assert rep.passed is False
    assert rep.api_error["status_code"] == 400
    assert "tool_choice" in rep.api_error["message"]


# ---------------------------------------------------------------------------
# The negative: an OpenAI repo must never come out Anthropic
# ---------------------------------------------------------------------------


def test_an_openai_repo_is_not_classified_as_messages(tmp_path):
    """The fixture has no Anthropic signal at all: nothing in the ranking, the AST, or the
    gate may turn it into a Messages API agent."""
    _, inv = inventory_of(HANDROLLED, tmp_path)
    reasons = " ".join(r for f in inv.files for r in f.reasons)
    assert "messages.create call" not in reasons
    assert "anthropic" not in reasons.lower()
    assert not [s for s in inv.call_sites if s.callee.endswith("messages.create")]

    claimed = json.loads(json.dumps(EXTRACTION))
    claimed["endpoint"] = {"value": "messages", "citation": "support_agent.py:74",
                           "status": "found", "note": ""}
    verification = verify(claimed, HANDROLLED)
    assert verification.data["endpoint"]["verified"] is False, (
        "chat.completions.create must never satisfy a 'messages' endpoint claim"
    )
    assert any(
        "no 'messages' call marker" in f.reason for f in verification.flags_for("agent.json")
    )
