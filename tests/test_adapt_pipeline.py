"""`upshift adapt` stages 4-5 end to end: generate, report, and the CLI command.

The scripted extraction below is what a *good* model reply looks like for
`tests/adapt_fixtures/handrolled`, with one deliberately hallucinated verbatim chunk and one
tool whose semantics are not mechanical. The generated directory then has to survive
everything a real agent directory has to survive: cli.validate_agent_dir, importing
backend.py, and a full sim run of one of its own cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift import cli
from upshift.adapt.generate import PLACEHOLDER_TOOL_NAME, generate, slugify
from upshift.adapt.inventory import resolve_source, take_inventory
from upshift.adapt.report import CostInfo, render_report
from upshift.adapt.verify import Verification, verify
from upshift.providers.sim import SimProvider
from upshift.recorder import load_case_reps
from upshift.runner import load_backend_factory, run_suite
from upshift.schemas import Case

ROOT = Path(__file__).resolve().parents[1]
HANDROLLED = ROOT / "tests" / "adapt_fixtures" / "handrolled"

HALLUCINATED_CHUNK = "Always escalate to a human supervisor before issuing a refund."

EXTRACTION = {
    "agent_name": "Orderly Support",
    "endpoint": {"value": "chat_completions", "citation": "support_agent.py:74",
                 "status": "found", "note": ""},
    "model": {"value": "gpt-4o-mini", "citation": "support_agent.py:75", "status": "found",
              "note": ""},
    "params": {"value": {"temperature": 0.2, "stream": True},
               "citation": "support_agent.py:78", "status": "found", "note": ""},
    "max_turns": {"value": None, "citation": "", "status": "undetermined",
                  "note": "the loop is unbounded (support_agent.py:71)"},
    "system_prompt": {
        "status": "found",
        "note": "three adjacent string literals",
        "chunks": [
            {"text": "You are Orderly, a support assistant for an online store.",
             "kind": "verbatim", "citation": "support_agent.py:10", "note": ""},
            {"text": "Look orders up before you answer, and never invent an order id.",
             "kind": "verbatim", "citation": "support_agent.py:11", "note": ""},
            {"text": "Keep replies to two sentences.", "kind": "verbatim",
             "citation": "support_agent.py:12", "note": ""},
            {"text": HALLUCINATED_CHUNK, "kind": "verbatim", "citation": "support_agent.py:13",
             "note": "invented; must not survive the gate"},
        ],
    },
    "tools": [
        {
            "name": "lookup_order",
            "description": "Look up one order by its id.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "The order id."}},
                "required": ["order_id"],
            },
            "kind": "verbatim",
            "citation": "support_agent.py:19",
            "backend": {"kind": "lookup", "state_key": "orders", "id_field": "order_id",
                        "id_prefix": "", "match_fields": ["order_id"], "text_field": "",
                        "citation": "support_agent.py:51",
                        "reason": "pure dict lookup over ORDERS"},
        },
        {
            "name": "refund_order",
            "description": "Refund an order that has already shipped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order id."},
                    "reason": {"type": "string", "description": "Why the refund is issued."},
                },
                "required": ["order_id"],
            },
            "kind": "verbatim",
            "citation": "support_agent.py:31",
            "backend": {"kind": "unclear", "state_key": "", "id_field": "", "id_prefix": "",
                        "match_fields": [], "text_field": "", "citation": "support_agent.py:57",
                        "reason": "the refund's money side is not modelled in this repo"},
        },
    ],
    "cases": [
        {
            "id": "lookup_shipped_order",
            "description": "README status example.",
            "user_messages": ["Where is order A-1001?"],
            "initial_state": {"orders": [
                {"order_id": "A-1001", "status": "shipped", "total": 42.0},
                {"order_id": "A-1002", "status": "processing", "total": 12.5},
            ]},
            "expected_tool_calls": [{"name": "lookup_order", "arguments": {"order_id": "A-1001"}}],
            "final_message": "Order A-1001 has shipped.",
            "checks": [{"type": "response_contains", "text": "shipped"}],
            "citation": "README.md:9",
            "note": "",
        },
        {
            "id": "refund_after_lookup",
            "description": "README refund example.",
            "user_messages": ["Refund order A-1001, it arrived damaged."],
            "initial_state": {"orders": [{"order_id": "A-1001", "status": "shipped"}]},
            "expected_tool_calls": [
                {"name": "lookup_order", "arguments": {"order_id": "A-1001"}},
                {"name": "refund_order", "arguments": {"order_id": "A-1001",
                                                       "reason": "arrived damaged"}},
            ],
            "final_message": "I refunded order A-1001.",
            "checks": [{"type": "tool_called", "name": "refund_order", "max_times": 1},
                       {"type": "tool_called", "name": "send_apology_email"},
                       {"type": "vibes_are_good"}],
            "citation": "README.md:16",
            "note": "",
        },
    ],
    "undetermined": [
        {"what": "max_turns", "pointer": "support_agent.py:71",
         "why": "the while loop has no bound"},
    ],
    "notes": "",
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("adapt")
    repo = resolve_source(str(HANDROLLED), tmp)
    verification = verify(EXTRACTION, repo.root)
    out_dir = tmp / "orderly_agent"
    result = generate(verification, out_dir, origin=str(HANDROLLED), commit="abc1234")
    return verification, result, out_dir


# ---------------------------------------------------------------------------
# The five files
# ---------------------------------------------------------------------------


def test_every_file_the_contract_requires_is_written(generated):
    _, result, out_dir = generated
    assert set(result.files) == {
        "agent.json", "system_prompt.txt", "tools.json", "backend.py", "cases/cases.json",
        "PROVENANCE.json",
    }
    for rel in result.files:
        assert (out_dir / rel).is_file()


def test_the_generated_directory_satisfies_the_adapter_contract(generated):
    _, _, out_dir = generated
    config = cli.validate_agent_dir(out_dir)  # raises ValueError on anything malformed
    assert config["endpoint"] == "chat_completions"
    assert config["model"] == "gpt-4o-mini"
    assert config["name"] == slugify("Orderly Support") == "orderly-support"


def test_the_prompt_is_the_verified_source_lines_and_nothing_else(generated):
    _, _, out_dir = generated
    text = (out_dir / "system_prompt.txt").read_text()
    assert text == (
        "You are Orderly, a support assistant for an online store.\n"
        "Look orders up before you answer, and never invent an order id.\n"
        "Keep replies to two sentences.\n"
    )
    assert HALLUCINATED_CHUNK not in text, "a downgraded chunk is still written, but flagged"
    # The prompt is sent to the model verbatim, so it carries no adapt commentary.
    assert "adapt" not in text.lower()


def test_the_hallucinated_chunk_is_flagged_not_silently_kept(generated):
    verification, result, _ = generated
    chunk = verification.data["system_prompt"]["chunks"][3]
    assert chunk["kind"] == "inferred"
    assert verification.confidence["system_prompt.txt"] == "medium"
    reviews = [m for m in result.must_review if m.file == "system_prompt.txt"]
    assert reviews, "the downgraded chunk must produce a must-review line"
    omission = next(m for m in reviews if m.reason.startswith("OMITTED"))
    assert "support_agent.py:13" in omission.reason
    assert HALLUCINATED_CHUNK in omission.reason, "the omitted text is quoted, not just counted"
    # PROVENANCE.json is the machine-readable half of the same statement.
    _, _, out_dir = generated
    provenance = json.loads((out_dir / "PROVENANCE.json").read_text())
    omitted = [c for c in provenance["system_prompt"] if c["omitted"]]
    assert [c["citation"] for c in omitted] == ["support_agent.py:13"]


def test_tools_json_is_plain_chat_style_with_no_adapt_keys(generated):
    _, _, out_dir = generated
    tools = json.loads((out_dir / "tools.json").read_text())
    assert [t["function"]["name"] for t in tools] == ["lookup_order", "refund_order"]
    for tool in tools:
        assert set(tool) == {"type", "function"}
        assert set(tool["function"]) == {"name", "description", "parameters"}


def test_blocked_and_unverified_params_never_reach_agent_json(generated):
    _, result, out_dir = generated
    config = json.loads((out_dir / "agent.json").read_text())
    assert config["params"] == {"temperature": 0.2}
    assert any("stream" in note for note in result.notes)
    assert config["max_turns"] == 12  # undetermined -> the documented default


# ---------------------------------------------------------------------------
# backend.py
# ---------------------------------------------------------------------------


def test_backend_imports_and_honours_the_never_raises_contract(generated):
    _, _, out_dir = generated
    backend = load_backend_factory(out_dir)({"orders": [{"order_id": "A-1001",
                                                         "status": "shipped"}]})
    assert backend.execute("lookup_order", {"order_id": "A-1001"}) == {
        "results": [{"order_id": "A-1001", "status": "shipped"}]
    }
    assert backend.execute("lookup_order", {"order_id": "ghost"}) == {"results": []}
    # Unknown tool, wrong argument type, and a stubbed tool are all *results*, never raises.
    assert "unknown tool" in backend.execute("nope", {})["error"]
    assert "must be an object" in backend.execute("lookup_order", "A-1001")["error"]
    stub = backend.execute("refund_order", {"order_id": "A-1001"})
    assert stub["error"].startswith("TODO(adapt): not implemented — ")
    assert "money side" in stub["error"]
    assert backend.state() == {"orders": [{"order_id": "A-1001", "status": "shipped"}]}


def test_backend_is_deterministic_and_does_not_share_state(generated):
    _, _, out_dir = generated
    factory = load_backend_factory(out_dir)
    initial = {"orders": [{"order_id": "A-1001", "status": "shipped"}]}
    first, second = factory(initial), factory(initial)
    assert first.execute("lookup_order", {"order_id": "A-1001"}) == second.execute(
        "lookup_order", {"order_id": "A-1001"}
    )
    first.state()["orders"].append({"order_id": "X"})
    assert len(second.state()["orders"]) == 1
    assert initial["orders"] == [{"order_id": "A-1001", "status": "shipped"}]


def test_backend_carries_file_line_provenance_and_a_review_warning(generated):
    _, result, out_dir = generated
    source = (out_dir / "backend.py").read_text()
    assert "REVIEW BEFORE USE" in source
    assert "lookup_order <- support_agent.py:51  [lookup] REVIEW" in source
    assert "refund_order <- support_agent.py:57  [stub: TODO]" in source
    assert result.implemented_tools == ["lookup_order"]
    assert result.stub_tools == ["refund_order"]
    reviews = {m.reason for m in result.must_review if m.file == "backend.py"}
    assert any("re-implementation" in r for r in reviews)
    assert any("TODO stub" in r for r in reviews)


@pytest.mark.parametrize(
    ("kind", "spec", "call", "expected"),
    [
        ("list", {"state_key": "notes"}, {}, {"results": [{"note_id": "N-1"}]}),
        ("create", {"state_key": "notes", "id_field": "note_id", "id_prefix": "N-"},
         {"title": "x"}, {"title": "x", "note_id": "N-1002"}),
        ("update", {"state_key": "notes", "id_field": "note_id"},
         {"note_id": "N-1", "done": True}, {"note_id": "N-1", "done": True}),
    ],
)
def test_the_mechanical_backend_kinds(tmp_path, kind, spec, call, expected):
    data = {
        "agent_name": "x", "endpoint": {"value": "chat_completions"}, "model": {"value": "m"},
        "params": {"value": {}}, "max_turns": {"value": 4},
        "system_prompt": {"status": "found", "chunks": [
            {"text": "hi", "kind": "inferred", "citation": "a.py:1"}]},
        "tools": [{"name": "t", "description": "", "parameters": {},
                   "kind": "inferred", "citation": "a.py:1",
                   "backend": {"kind": kind, **spec}}],
        "cases": [], "undetermined": [],
    }
    out_dir = tmp_path / f"agent_{kind}"
    generate(Verification(data=data), out_dir, origin="fixture", commit=None)
    backend = load_backend_factory(out_dir)({"notes": [{"note_id": "N-1"}]})
    assert backend.execute("t", call) == expected


def test_file_tool_kinds_round_trip(tmp_path):
    data = {
        "agent_name": "x", "endpoint": {"value": "chat_completions"}, "model": {"value": "m"},
        "params": {"value": {}}, "max_turns": {"value": 4},
        "system_prompt": {"status": "found", "chunks": []},
        "tools": [
            {"name": "read_file", "description": "", "parameters": {}, "kind": "inferred",
             "citation": "a.py:1",
             "backend": {"kind": "file_read", "match_fields": ["path"]}},
            {"name": "write_file", "description": "", "parameters": {}, "kind": "inferred",
             "citation": "a.py:1",
             "backend": {"kind": "file_write", "match_fields": ["path"], "text_field": "body"}},
        ],
        "cases": [], "undetermined": [],
    }
    out_dir = tmp_path / "files_agent"
    generate(Verification(data=data), out_dir, origin="fixture", commit=None)
    backend = load_backend_factory(out_dir)({"files": {"a.txt": "hello"}})
    assert backend.execute("read_file", {"path": "a.txt"}) == {"path": "a.txt", "content": "hello"}
    assert backend.execute("read_file", {"path": "b.txt"})["error"] == "no such file: b.txt"
    assert backend.execute("write_file", {"path": "b.txt", "body": "new"})["written"] is True
    assert backend.state()["files"]["b.txt"] == "new"


# ---------------------------------------------------------------------------
# cases/cases.json
# ---------------------------------------------------------------------------


def test_cases_are_loadable_carry_provenance_and_an_oracle_plan(generated):
    _, result, out_dir = generated
    cases = Case.load_all(out_dir / "cases" / "cases.json")
    assert [c.id for c in cases] == ["lookup_shipped_order", "refund_after_lookup"]
    first = cases[0]
    assert "[adapt: derived from README.md:9]" in first.description
    assert first.sim["oracle_plan"][0]["tool_calls"][0] == {
        "name": "lookup_order", "arguments": {"order_id": "A-1001"}
    }
    assert first.sim["oracle_plan"][-1]["final_message"] == "Order A-1001 has shipped."
    assert first.sim["critical_tool"] == "lookup_order"
    assert result.case_ids == ["lookup_shipped_order", "refund_after_lookup"]


def test_only_supported_checks_over_known_tools_survive(generated):
    _, result, out_dir = generated
    cases = {c["id"]: c for c in json.loads((out_dir / "cases" / "cases.json").read_text())}
    kinds = [c["type"] for c in cases["refund_after_lookup"]["checks"]]
    assert kinds.count("no_api_error") == 1
    assert "vibes_are_good" not in kinds, "unknown check types are dropped, never passed through"
    named = {c.get("name") for c in cases["refund_after_lookup"]["checks"]
             if c["type"] == "tool_called"}
    assert named == {"lookup_order", "refund_order"}
    assert "send_apology_email" not in str(cases["refund_after_lookup"])
    assert any("unsupported check type" in n for n in result.notes)
    assert any("unknown tool" in n for n in result.notes)


def test_a_case_expecting_a_tool_that_does_not_exist_is_dropped_whole(tmp_path):
    extraction = json.loads(json.dumps(EXTRACTION))
    extraction["cases"][0]["expected_tool_calls"] = [{"name": "teleport", "arguments": {}}]
    verification = verify(extraction, HANDROLLED)
    result = generate(verification, tmp_path / "agent", origin="fixture", commit=None)
    assert result.case_ids == ["refund_after_lookup"]
    assert result.dropped_cases[0][0] == "lookup_shipped_order"
    assert "teleport" in result.dropped_cases[0][1]


def test_a_generated_case_runs_on_the_sim_provider(generated, tmp_path):
    """The whole point of sim.oracle_plan: `upshift upgrade --provider sim` runs unchanged."""
    _, _, out_dir = generated
    runs_root = tmp_path / "runs"
    run_suite(
        out_dir, SimProvider(), "adapt-smoke", n_reps=2, model_override="sim-5.5",
        runs_root=runs_root, workers=1, case_ids=["lookup_shipped_order"],
    )
    reps = load_case_reps(runs_root / "adapt-smoke", "lookup_shipped_order")
    assert len(reps) == 2
    assert all(r.passed for r in reps), [
        (c.check, c.detail) for r in reps for c in r.check_results if not c.passed
    ]
    assert [t.name for t in reps[0].tool_executions] == ["lookup_order"]
    assert reps[0].tool_executions[0].result == {
        "results": [{"order_id": "A-1001", "status": "shipped", "total": 42.0}]
    }


def test_the_documented_break_still_fires_on_the_generated_agent(generated, tmp_path):
    """sim-5.6-sol must reject the generated chat_completions agent exactly as it rejects a
    hand-written one — the generated directory is not special-cased anywhere."""
    _, _, out_dir = generated
    runs_root = tmp_path / "runs"
    run_suite(
        out_dir, SimProvider(), "adapt-candidate", n_reps=1, model_override="sim-5.6-sol",
        runs_root=runs_root, workers=1, case_ids=["lookup_shipped_order"],
    )
    rep = load_case_reps(runs_root / "adapt-candidate", "lookup_shipped_order")[0]
    assert rep.passed is False
    assert rep.api_error["status_code"] == 400


# ---------------------------------------------------------------------------
# Nothing extracted: holes, not guesses
# ---------------------------------------------------------------------------


def test_an_empty_extraction_produces_todos_and_a_loud_report(tmp_path):
    from upshift.adapt.extract import EMPTY_EXTRACTION

    verification = verify(dict(EMPTY_EXTRACTION), HANDROLLED)
    out_dir = tmp_path / "empty_agent"
    result = generate(verification, out_dir, origin="fixture", commit=None)
    prompt = (out_dir / "system_prompt.txt").read_text()
    tools = json.loads((out_dir / "tools.json").read_text())
    assert prompt.startswith("TODO(adapt):")
    assert tools[0]["function"]["name"] == PLACEHOLDER_TOOL_NAME
    assert json.loads((out_dir / "cases" / "cases.json").read_text()) == []
    assert all(level == "low" for level in verification.confidence.values())
    files = {m.file for m in result.must_review}
    assert files == {"system_prompt.txt", "tools.json", "backend.py", "cases/cases.json",
                     "agent.json"}
    backend = load_backend_factory(out_dir)({})
    assert "TODO(adapt)" in backend.execute(PLACEHOLDER_TOOL_NAME, {})["error"]


# ---------------------------------------------------------------------------
# ADAPT_REPORT.md
# ---------------------------------------------------------------------------


def test_the_report_says_what_was_found_inferred_and_undetermined(generated, tmp_path):
    verification, result, out_dir = generated
    repo = resolve_source(str(HANDROLLED), tmp_path)
    inventory = take_inventory(repo)
    text = render_report(
        inventory=inventory,
        extraction=None,
        verification=verification,
        generation=result,
        cost=CostInfo("openai-flex", "gpt-5.5", 40_000, 3_000, 0, 0.145, "/runs/adapt-x",
                      "adapt-x"),
        out_dir=out_dir,
    )
    for heading in ("## Confidence per artifact", "## Found", "## Inferred", "## Undetermined",
                    "## Must review before a real run", "## Verification gate",
                    "## Evidence the extraction saw", "## Cost", "## Next"):
        assert heading in text, heading
    assert "verbatim-verified against the cited source" in text
    assert "support_agent.py:13" in text  # the hallucinated citation is named
    assert "max_turns" in text and "support_agent.py:71" in text  # the undetermined pointer
    assert "$0.1450" in text
    assert "upshift upgrade --agent" in text
    assert "`tools.json` | high" in text
    assert "`backend.py` | low" in text  # a stub is never dressed up


def test_a_partial_report_is_written_when_the_run_aborts(tmp_path):
    repo = resolve_source(str(HANDROLLED), tmp_path)
    text = render_report(
        inventory=take_inventory(repo), extraction=None, verification=None, generation=None,
        cost=None, out_dir=tmp_path / "out", aborted="over --max-cost-usd 2.00",
    )
    assert "## ABORTED" in text
    assert "over --max-cost-usd 2.00" in text
    assert "This report is partial" in text
    assert "## Evidence the extraction saw" in text  # what we did learn is still reported


# ---------------------------------------------------------------------------
# The CLI command
# ---------------------------------------------------------------------------


class ScriptedProvider:
    name = "openai"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def call(self, endpoint, request, seed_key, sim_context=None):
        self.calls.append(request)
        return {
            "model": "gpt-5.5",
            "choices": [{"message": {"role": "assistant", "content": self.reply}}],
            "usage": {"prompt_tokens": 12_000, "completion_tokens": 900,
                      "prompt_tokens_details": {"cached_tokens": 0}},
        }


def test_the_cli_writes_an_agent_directory_a_report_and_a_priced_run(tmp_path, monkeypatch):
    provider = ScriptedProvider(json.dumps(EXTRACTION))
    monkeypatch.setattr(cli, "_make_provider", lambda args: provider)
    out_dir = tmp_path / "orderly"
    code = cli.main([
        "adapt", str(HANDROLLED), "--out", str(out_dir),
        "--runs-root", str(tmp_path / "runs"), "--agent-hint", "default mode",
    ])
    assert code == 0
    assert (out_dir / "ADAPT_REPORT.md").is_file()
    cli.validate_agent_dir(out_dir)
    assert "default mode" in provider.calls[0]["messages"][1]["content"]
    run_directory = tmp_path / "runs" / "adapt-orderly"
    assert json.loads((run_directory / "manifest.json").read_text())["provider"] == "openai"
    assert cli.main(["cost", "adapt-orderly", "--runs-root", str(tmp_path / "runs")]) == 0


FRAMEWORK = ROOT / "tests" / "adapt_fixtures" / "framework_flavored"

FRAMEWORK_EXTRACTION = {
    "agent_name": "notesbot",
    "endpoint": {"value": "chat_completions", "citation": "notesbot/app.py:34",
                 "status": "found", "note": "litellm.completion is chat/completions"},
    "model": {"value": "gpt-4.1", "citation": "notesbot/app.py:12", "status": "found",
              "note": "MODEL constant, passed at notesbot/app.py:35"},
    "params": {"value": {"tool_choice": "auto", "temperature": 0.0},
               "citation": "notesbot/app.py:38", "status": "found", "note": ""},
    "max_turns": {"value": 8, "citation": "notesbot/app.py:48", "status": "found", "note": ""},
    "system_prompt": {
        "status": "found", "note": "template rendered with PRODUCT_NAME",
        "chunks": [
            {"text": "You are Notebook, the note-taking assistant inside Ledger.",
             "kind": "templated", "citation": "prompts/notes_role.txt:1",
             "note": "{product} <- PRODUCT_NAME = 'Ledger' (notesbot/app.py:11)"},
            {"text": "Save a note whenever the user tells you something worth remembering.",
             "kind": "verbatim", "citation": "prompts/notes_role.txt:2", "note": ""},
            {"text": "Read the notes back before you claim a note exists.",
             "kind": "verbatim", "citation": "prompts/notes_role.txt:3", "note": ""},
            {"text": "Answer in one short paragraph.", "kind": "verbatim",
             "citation": "prompts/notes_role.txt:4", "note": ""},
        ],
    },
    "tools": [
        {"name": "save_note", "description": "Save one note for the user.",
         "parameters": {"type": "object",
                        "properties": {"title": {"type": "string"},
                                       "body": {"type": "string"}},
                        "required": ["title"]},
         "kind": "templated", "citation": "notesbot/tools.py:7",
         "backend": {"kind": "create", "state_key": "notes", "id_field": "note_id",
                     "id_prefix": "N-", "match_fields": [], "text_field": "",
                     "citation": "notesbot/app.py:24", "reason": "appends a dict with N-<seq>"}},
        {"name": "list_notes", "description": "List every note the user has saved.",
         "parameters": {"type": "object", "properties": {}, "required": []},
         "kind": "templated", "citation": "notesbot/tools.py:15",
         "backend": {"kind": "list", "state_key": "notes", "id_field": "", "id_prefix": "",
                     "match_fields": [], "text_field": "", "citation": "notesbot/app.py:29",
                     "reason": "returns the whole list"}},
    ],
    "cases": [
        {"id": "save_then_list", "description": "README usage block.",
         "user_messages": ["Remember that the office wifi password is hunter2.",
                           "What notes do I have?"],
         "initial_state": {"notes": []},
         "expected_tool_calls": [
             {"name": "save_note", "arguments": {"title": "office wifi password",
                                                 "body": "hunter2"}},
             {"name": "list_notes", "arguments": {}},
         ],
         "final_message": "You have one note: office wifi.",
         "checks": [{"type": "tool_called", "name": "save_note", "max_times": 1},
                    {"type": "state_count", "path": "notes", "equals": 1}],
         "citation": "README.md:10", "note": ""},
    ],
    "undetermined": [],
    "notes": "",
}


def test_a_framework_flavored_repo_produces_a_runnable_agent(tmp_path, monkeypatch):
    """Prompt from a template file, tools built from a dict, litellm as the transport — the
    generated directory still has to pass the contract and run on the sim."""
    provider = ScriptedProvider(json.dumps(FRAMEWORK_EXTRACTION))
    monkeypatch.setattr(cli, "_make_provider", lambda args: provider)
    out_dir = tmp_path / "notesbot"
    runs_root = tmp_path / "runs"
    assert cli.main([
        "adapt", str(FRAMEWORK), "--out", str(out_dir), "--runs-root", str(runs_root)
    ]) == 0

    config = cli.validate_agent_dir(out_dir)
    assert config["endpoint"] == "chat_completions"  # litellm.completion is chat/completions
    assert config["model"] == "gpt-4.1"
    assert config["max_turns"] == 8
    assert json.loads((out_dir / "agent.json").read_text())["params"] == {
        "tool_choice": "auto", "temperature": 0.0
    }
    prompt = (out_dir / "system_prompt.txt").read_text()
    assert prompt.startswith("You are Notebook, the note-taking assistant inside Ledger.")
    assert prompt.endswith("Answer in one short paragraph.\n")

    run_suite(
        out_dir, SimProvider(), "notes-smoke", n_reps=1, model_override="sim-5.5",
        runs_root=runs_root, workers=1,
    )
    rep = load_case_reps(runs_root / "notes-smoke", "save_then_list")[0]
    assert rep.passed, [(c.check, c.detail) for c in rep.check_results if not c.passed]
    assert [t.name for t in rep.tool_executions] == ["save_note", "list_notes"]
    assert rep.final_state["notes"] == [
        {"title": "office wifi password", "body": "hunter2", "note_id": "N-1001"}
    ]
    # The templated prompt line is grounded on an anchor, so the whole prompt is medium.
    provenance = json.loads((out_dir / "PROVENANCE.json").read_text())
    assert provenance["confidence"]["system_prompt.txt"] == "medium"
    assert provenance["system_prompt"][0]["kind"] == "templated"


def test_the_cli_refuses_a_non_empty_output_directory(tmp_path, capsys):
    out_dir = tmp_path / "taken"
    out_dir.mkdir()
    (out_dir / "agent.json").write_text("{}")
    assert cli.main(["adapt", str(HANDROLLED), "--out", str(out_dir)]) == 2
    assert "already exists and is not" in capsys.readouterr().out.replace("\n", " ")


def test_the_cli_reports_a_repo_with_no_agent_in_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_make_provider", lambda args: ScriptedProvider("{}"))
    empty = tmp_path / "boring"
    empty.mkdir()
    (empty / "notes.md").write_text("nothing to see here\n")
    assert cli.main(["adapt", str(empty), "--out", str(tmp_path / "out")]) == 2
    assert "nothing here" in capsys.readouterr().out


def test_a_blown_budget_leaves_a_partial_report_and_exit_2(tmp_path, monkeypatch, capsys):
    provider = ScriptedProvider(json.dumps(EXTRACTION))
    monkeypatch.setattr(cli, "_make_provider", lambda args: provider)
    out_dir = tmp_path / "broke"
    code = cli.main([
        "adapt", str(HANDROLLED), "--out", str(out_dir), "--max-cost-usd", "0.000001",
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert code == 2
    assert provider.calls == []
    report = (out_dir / "ADAPT_REPORT.md").read_text()
    assert "## ABORTED" in report
    assert "--max-cost-usd" in report
    assert not (out_dir / "agent.json").exists(), "no half-written agent directory"
    assert "partial report" in capsys.readouterr().out
