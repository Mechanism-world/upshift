"""`upshift adapt` stages 1-3: inventory, extraction engine, verification gate.

Every test here is offline and free: the extraction engine is injectable, so a scripted
`call_model` stands in for the provider, and the one git-clone path is exercised with a
local directory copy instead of a network clone.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from upshift.adapt import AdaptAborted
from upshift.adapt.extract import (
    ExtractionResult,
    build_messages,
    extract,
    parse_json_object,
    response_text,
    validate_extraction,
)
from upshift.adapt.inventory import (
    analyze_python,
    analyze_regex,
    estimate_tokens,
    is_git_url,
    render_evidence,
    resolve_source,
    take_inventory,
)
from upshift.adapt.record import RecordingExtractor
from upshift.adapt.verify import HIGH, LOW, MEDIUM, normalize_ws, parse_citation, verify
from upshift.providers.base import Provider, ProviderAPIError

ROOT = Path(__file__).resolve().parents[1]
HANDROLLED = ROOT / "tests" / "adapt_fixtures" / "handrolled"
FRAMEWORK = ROOT / "tests" / "adapt_fixtures" / "framework_flavored"


def inventory_of(path: Path, tmp_path: Path, **kwargs):
    repo = resolve_source(str(path), tmp_path)
    return repo, take_inventory(repo, **kwargs)


# ---------------------------------------------------------------------------
# Stage 1: inventory
# ---------------------------------------------------------------------------


def test_ranks_the_agent_file_first(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    assert inv.files[0].path == "support_agent.py"
    assert inv.files[0].score > 20
    assert {f.path for f in inv.files} == {"support_agent.py", "README.md"}


def test_ast_finds_the_call_site_with_model_and_tools(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    sites = [s for s in inv.call_sites if s.callee.endswith("chat.completions.create")]
    assert len(sites) == 1
    site = sites[0]
    assert site.how == "ast"
    assert site.path == "support_agent.py"
    assert site.kwargs["model"] == "'gpt-4o-mini'"
    assert site.kwargs["tools"] == "TOOLS"
    assert site.kwargs["temperature"] == "0.2"
    # The citation must point at a line that really holds the call.
    line = (HANDROLLED / "support_agent.py").read_text().splitlines()[site.line - 1]
    assert "chat.completions.create" in line


def test_ast_finds_the_prompt_constant(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    names = {p.name for p in inv.prompt_constants}
    assert "SYSTEM_PROMPT" in names


def test_framework_flavored_repo_surfaces_template_and_tool_builder(tmp_path):
    _, inv = inventory_of(FRAMEWORK, tmp_path)
    ranked = [f.path for f in inv.files]
    assert ranked[0] == "notesbot/app.py"
    assert "notesbot/tools.py" in ranked
    assert "prompts/notes_role.txt" in ranked  # a prompt that is not in any .py file
    call = next(s for s in inv.call_sites if "litellm" in s.callee)
    assert call.kwargs["model"] == "MODEL"  # a name, not a literal: extraction must resolve it
    assert call.kwargs["tools"] == "build_tools()"


def test_regex_fallback_covers_non_python_sources():
    text = "const r = await client.chat.completions.create({model: 'gpt-4o'});\n"
    sites = analyze_regex("app.js", text)
    assert [(s.line, s.how) for s in sites] == [(1, "regex")]
    assert sites[0].callee.endswith("chat.completions.create")


def test_python_syntax_errors_do_not_stop_the_walk():
    calls, prompts, extra = analyze_python("broken.py", "def f(:\n  pass\n")
    assert (calls, prompts, extra) == ([], [], [])


def test_recorded_api_traffic_is_not_mistaken_for_source(tmp_path):
    """A saved response body is full of tool schemas and system messages and would outrank
    the code that produced it. Data files that look like transcripts are excluded; Python
    that merely mentions usage keys is not (that is the response-parsing code we want)."""
    repo_dir = tmp_path / "repo"
    (repo_dir / "fixtures").mkdir(parents=True)
    (repo_dir / "agent.py").write_text(
        'from openai import OpenAI\n'
        'SYSTEM_PROMPT = "You are a helpful assistant that books rooms."\n'
        'def go(c):\n'
        '    return c.chat.completions.create(model="gpt-4o", messages=[], tools=TOOLS)\n'
    )
    (repo_dir / "usage.py").write_text(
        'def parse(response):\n'
        '    """Reads tool_calls and prompt_tokens: source, not a transcript."""\n'
        '    calls = response["choices"][0]["message"].get("tool_calls") or []\n'
        '    return calls, response["usage"]["prompt_tokens"]\n'
    )
    (repo_dir / "fixtures" / "recorded_response.json").write_text(
        json.dumps({
            "id": "chatcmpl-123", "object": "chat.completion",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"type": "function",
                                "function": {"name": "book_room", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })
    )
    _, inv = inventory_of(repo_dir, tmp_path / "work")
    ranked = [f.path for f in inv.files]
    assert "agent.py" in ranked
    assert "usage.py" in ranked
    assert "fixtures/recorded_response.json" not in ranked
    assert all("recorded_response" not in s.path for s in inv.slices)


def test_files_beyond_the_cap_are_counted_not_hidden(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    for index in range(6):
        (repo_dir / f"agent_{index}.py").write_text(
            f'from openai import OpenAI\nMODEL = "gpt-4o"\n'
            f'def go(c):\n    return c.chat.completions.create(model=MODEL, tools=[{index}])\n'
        )
    _, inv = inventory_of(repo_dir, tmp_path / "work", max_files=2)
    assert len(inv.files) == 2
    assert inv.candidate_files == 6
    assert inv.truncated is False  # the file cap is not the token budget


def test_evidence_budget_is_a_hard_cap(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path, max_tokens=40)
    assert inv.truncated is True
    assert inv.evidence_tokens <= 40
    assert estimate_tokens(render_evidence(inv)) <= 200


def test_rendered_evidence_carries_paths_and_line_numbers(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    rendered = render_evidence(inv)
    assert "===== FILE support_agent.py lines 1-" in rendered
    assert "SYSTEM_PROMPT" in rendered
    # Gutter line numbers are what every citation in the extraction refers to.
    numbered = [line for line in rendered.splitlines() if "SYSTEM_PROMPT = (" in line]
    assert numbered and numbered[0].split("|")[0].strip().isdigit()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://github.com/TheR1D/shell_gpt", True),
        ("https://github.com/TheR1D/shell_gpt.git", True),
        ("git@github.com:TheR1D/shell_gpt.git", True),
        ("ssh://git@example.com/x.git", True),
        ("/Users/me/code/agent", False),
        ("./agent", False),
        ("agent.git", False),  # a local directory that happens to end in .git
    ],
)
def test_git_url_detection(source, expected):
    assert is_git_url(source) is expected


def test_git_url_is_cloned_into_a_temp_dir_and_the_commit_is_recorded(tmp_path):
    """The clone path, with a local copy standing in for the network."""
    calls = []

    def fake_clone(url: str, dest: Path) -> str:
        calls.append((url, dest))
        shutil.copytree(HANDROLLED, dest)
        return "deadbeefcafe0000000000000000000000000000"

    repo = resolve_source(
        "https://example.com/org/agent.git", tmp_path, clone_fn=fake_clone
    )
    assert repo.is_clone is True
    assert repo.commit == "deadbeefcafe0000000000000000000000000000"
    assert repo.root == tmp_path / "clone"
    assert calls == [("https://example.com/org/agent.git", tmp_path / "clone")]
    inv = take_inventory(repo)
    assert inv.files[0].path == "support_agent.py"


def test_local_path_errors_are_one_liners(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        resolve_source(str(tmp_path / "nope"), tmp_path)
    file_path = tmp_path / "a.py"
    file_path.write_text("x = 1\n")
    with pytest.raises(ValueError, match="not a directory"):
        resolve_source(str(file_path), tmp_path)


# ---------------------------------------------------------------------------
# Stage 2: the extraction engine (scripted)
# ---------------------------------------------------------------------------


def chat_response(content: str, *, prompt_tokens: int = 1000, completion_tokens: int = 200) -> dict:
    return {
        "id": "chatcmpl-fake",
        "model": "gpt-5.5-2026-01-01",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
    }


def minimal_extraction(**overrides) -> dict:
    data = {
        "agent_name": "orderly",
        "endpoint": {"value": "chat_completions", "citation": "support_agent.py:74",
                     "status": "found", "note": ""},
        "model": {"value": "gpt-4o-mini", "citation": "support_agent.py:75",
                  "status": "found", "note": ""},
        "params": {"value": {"temperature": 0.2}, "citation": "support_agent.py:78",
                   "status": "found", "note": ""},
        "max_turns": {"value": None, "citation": "", "status": "undetermined", "note": ""},
        "system_prompt": {"status": "found", "note": "", "chunks": []},
        "tools": [],
        "cases": [],
        "undetermined": [],
        "notes": "",
    }
    data.update(overrides)
    return data


def scripted(*replies: str):
    """A call_model that returns the given assistant texts in order."""
    seen: list[dict] = []

    def call_model(request: dict) -> dict:
        seen.append(request)
        return chat_response(replies[min(len(seen), len(replies)) - 1])

    call_model.requests = seen  # type: ignore[attr-defined]
    return call_model


def test_extraction_prompt_states_the_rules_and_carries_the_evidence(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    messages = build_messages(inv, agent_hint="the default mode only")
    system, user = messages[0]["content"], messages[1]["content"]
    assert "NEVER invent" in system
    assert "citation" in system
    assert "undetermined" in system
    assert "the default mode only" in user
    assert "===== FILE support_agent.py" in user


def test_one_clean_reply_is_accepted(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(json.dumps(minimal_extraction()))
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert result.ok is True
    assert len(result.attempts) == 1
    assert result.data["agent_name"] == "orderly"
    assert result.usage["input_tokens"] == 1000
    assert result.usage["cached_input_tokens"] == 100


def test_schema_violation_is_retried_exactly_once_and_both_attempts_are_kept(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    bad = json.dumps({"agent_name": "orderly"})  # missing everything else
    call_model = scripted(bad, json.dumps(minimal_extraction()))
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert result.ok is True
    assert [a.ok for a in result.attempts] == [False, True]
    # The retry shows the model its own reply and the exact violations.
    retry_messages = call_model.requests[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "did not satisfy the schema" in retry_messages[-1]["content"]
    assert "endpoint" in retry_messages[-1]["content"]


def test_two_violations_leave_a_flagged_salvage_not_a_crash(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(json.dumps({"agent_name": "orderly"}))
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert result.ok is False
    assert len(result.attempts) == 2
    assert result.errors
    assert result.data["agent_name"] == "orderly"  # salvaged
    assert result.data["tools"] == []


def test_a_reply_that_is_not_json_is_reported_as_such(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    result = extract(inv, call_model=scripted("I could not find an agent."), model="gpt-5.5")
    assert result.ok is False
    assert any("no JSON object" in e for e in result.errors)


def test_a_fenced_reply_is_still_parsed():
    data, error = parse_json_object('```json\n{"a": 1}\n```')
    assert (data, error) == ({"a": 1}, None)


def test_response_text_reads_both_endpoint_shapes():
    assert response_text(chat_response("hi")) == "hi"
    responses_shape = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "yo"}]}]
    }
    assert response_text(responses_shape) == "yo"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"endpoint": {"value": "assistants", "citation": "a.py:1", "status": "found"}},
         "endpoint.value"),
        ({"tools": [{"name": "x", "kind": "verbatim", "citation": "nope", "parameters": {},
                     "backend": {"kind": "list"}}]}, "citation"),
        ({"tools": [{"name": "x", "kind": "verbatim", "citation": "a.py:1", "parameters": {},
                     "backend": {"kind": "teleport"}}]}, "backend.kind"),
        ({"cases": [{"id": "a", "user_messages": [], "citation": "a.py:1"}]}, "user_messages"),
        ({"cases": [{"id": "a", "user_messages": ["hi"], "citation": ""}]}, "citation"),
    ],
)
def test_validator_catches_the_violations_that_matter(mutation, expected):
    errors = validate_extraction(minimal_extraction(**mutation))
    assert any(expected in e for e in errors), errors


def test_duplicate_names_are_violations():
    tool = {"name": "x", "kind": "verbatim", "citation": "a.py:1", "parameters": {},
            "backend": {"kind": "list"}}
    errors = validate_extraction(minimal_extraction(tools=[tool, dict(tool)]))
    assert any("duplicate tool name" in e for e in errors)


# ---------------------------------------------------------------------------
# Stage 3: the verification gate
# ---------------------------------------------------------------------------

REAL_LINE_1 = "You are Orderly, a support assistant for an online store."
REAL_LINE_2 = "Look orders up before you answer, and never invent an order id."
HALLUCINATED = "Always escalate to a human supervisor before issuing any refund."


def prompt_extraction(chunks: list[dict]) -> dict:
    return minimal_extraction(
        system_prompt={"status": "found", "note": "", "chunks": chunks}
    )


def test_whitespace_is_the_only_normalisation():
    assert normalize_ws("a\n   b\t c ") == "a b c"
    assert normalize_ws("A b") != normalize_ws("a b")  # case is not normalised


@pytest.mark.parametrize(
    ("citation", "expected"),
    [("a/b.py:12", ("a/b.py", 12, 12)), ("a.py:3-9", ("a.py", 3, 9)), ("a.py", None),
     ("", None), ("a.py:x", None)],
)
def test_citation_parsing(citation, expected):
    assert parse_citation(citation) == expected


def test_a_true_verbatim_claim_passes_the_gate():
    data = prompt_extraction(
        [{"text": REAL_LINE_1, "kind": "verbatim", "citation": "support_agent.py:10", "note": ""}]
    )
    result = verify(data, HANDROLLED)
    chunk = result.data["system_prompt"]["chunks"][0]
    assert chunk["verified"] is True
    assert chunk["kind"] == "verbatim"
    assert result.confidence["system_prompt.txt"] == HIGH
    assert result.downgraded == 0


def test_a_hallucinated_verbatim_claim_is_downgraded_and_flagged():
    data = prompt_extraction(
        [
            {"text": REAL_LINE_1, "kind": "verbatim", "citation": "support_agent.py:10",
             "note": ""},
            {"text": HALLUCINATED, "kind": "verbatim", "citation": "support_agent.py:11",
             "note": ""},
        ]
    )
    result = verify(data, HANDROLLED)
    good, bad = result.data["system_prompt"]["chunks"]
    assert good["kind"] == "verbatim" and good["verified"] is True
    assert bad["kind"] == "inferred", "a false verbatim claim must not survive the gate"
    assert bad["verified"] is False
    assert result.downgraded == 1
    flags = [f for f in result.flags_for("system_prompt") if f.severity == "error"]
    assert len(flags) == 1
    assert "claimed verbatim" in flags[0].reason
    assert flags[0].citation == "support_agent.py:11"
    # One verified chunk out of two: assembled from cited parts, not verbatim.
    assert result.confidence["system_prompt.txt"] == MEDIUM


def test_a_templated_chunk_is_anchored_rather_than_omitted():
    """The framework fixture fills `{product}` from config; the rendered text cannot match
    literally, so the gate grounds it on the longest run of words that IS in the template."""
    data = prompt_extraction(
        [{"text": "You are Notebook, the note-taking assistant inside Ledger.",
          "kind": "templated", "citation": "prompts/notes_role.txt:1",
          "note": "{product} filled from PRODUCT_NAME"}]
    )
    result = verify(data, FRAMEWORK)
    chunk = result.data["system_prompt"]["chunks"][0]
    assert chunk["verify"] == "anchored"
    assert chunk["omitted"] is False, "a templated prompt line must survive into the prompt"
    assert "You are Notebook, the note-taking assistant inside" in chunk["verify_detail"]
    assert result.confidence["system_prompt.txt"] == MEDIUM
    assert [f.severity for f in result.flags_for("system_prompt")] == ["warning"]


def test_a_verbatim_claim_that_only_partly_matches_is_downgraded_but_kept():
    data = prompt_extraction(
        [{"text": "You are Orderly, a support assistant for an online store and its warehouse.",
          "kind": "verbatim", "citation": "support_agent.py:10", "note": ""}]
    )
    result = verify(data, HANDROLLED)
    chunk = result.data["system_prompt"]["chunks"][0]
    assert chunk["kind"] == "templated"
    assert chunk["omitted"] is False
    assert result.downgraded == 1
    assert [f.severity for f in result.flags_for("system_prompt")] == ["error"]


def test_an_unfounded_parameter_caps_agent_json_confidence():
    data = minimal_extraction(
        params={"value": {"temperature": 0.2, "top_p": 0.9}, "citation": "support_agent.py:78",
                "status": "found", "note": ""}
    )
    result = verify(data, HANDROLLED)
    assert result.dropped_params == ["top_p"]
    assert result.data["params"]["verified"] is False
    assert result.confidence["agent.json"] == MEDIUM


def test_a_citation_pointing_at_a_missing_file_is_a_downgrade():
    data = prompt_extraction(
        [{"text": REAL_LINE_1, "kind": "verbatim", "citation": "no_such_file.py:1", "note": ""}]
    )
    result = verify(data, HANDROLLED)
    assert result.data["system_prompt"]["chunks"][0]["kind"] == "inferred"
    assert result.confidence["system_prompt.txt"] == LOW


def test_text_found_in_the_file_but_far_from_the_cited_line_is_kept_and_warned():
    data = prompt_extraction(
        [{"text": REAL_LINE_2, "kind": "verbatim", "citation": "support_agent.py:120", "note": ""}]
    )
    result = verify(data, HANDROLLED)
    chunk = result.data["system_prompt"]["chunks"][0]
    assert chunk["verified"] is True
    assert chunk["verify"] == "in_file"
    assert [f.severity for f in result.flags_for("system_prompt")] == ["warning"]


def test_tool_names_are_checked_against_the_cited_file():
    real = {"name": "lookup_order", "description": "Look up one order by its id.",
            "parameters": {"type": "object", "properties": {}}, "kind": "verbatim",
            "citation": "support_agent.py:22", "backend": {"kind": "unclear"}}
    fake = {"name": "cancel_subscription", "description": "Cancel it.",
            "parameters": {"type": "object", "properties": {}}, "kind": "verbatim",
            "citation": "support_agent.py:22", "backend": {"kind": "unclear"}}
    result = verify(minimal_extraction(tools=[real, fake]), HANDROLLED)
    assert result.data["tools"][0]["verified"] is True
    assert result.data["tools"][1]["kind"] == "inferred"
    assert result.confidence["tools.json"] == LOW
    reasons = [f.reason for f in result.flags_for("tools.json")]
    assert any("does not appear at its citation" in r for r in reasons)


def test_a_tool_whose_description_does_not_check_out_drops_to_templated():
    tool = {"name": "lookup_order", "description": "Looks up an order and emails the customer.",
            "parameters": {"type": "object", "properties": {}}, "kind": "verbatim",
            "citation": "support_agent.py:22", "backend": {"kind": "unclear"}}
    result = verify(minimal_extraction(tools=[tool]), HANDROLLED)
    assert result.data["tools"][0]["kind"] == "templated"
    assert result.confidence["tools.json"] == MEDIUM


def test_model_and_params_are_checked_and_unfounded_params_are_dropped():
    data = minimal_extraction(
        params={"value": {"temperature": 0.2, "reasoning_effort": "high"},
                "citation": "support_agent.py:78", "status": "found", "note": ""}
    )
    result = verify(data, HANDROLLED)
    assert result.data["model"]["verified"] is True
    assert result.data["params"]["value"] == {"temperature": 0.2}
    assert result.dropped_params == ["reasoning_effort"]
    assert any("dropped from agent.json" in f.reason for f in result.flags_for("agent.json"))


def test_a_fabricated_model_string_is_flagged():
    data = minimal_extraction(
        model={"value": "gpt-9-ultra", "citation": "support_agent.py:75", "status": "found",
               "note": ""}
    )
    result = verify(data, HANDROLLED)
    assert result.data["model"]["verified"] is False
    assert any(f.what == "model" and f.severity == "error" for f in result.flags_for("agent.json"))


def test_backend_semantics_are_never_high_confidence():
    tool = {"name": "lookup_order", "description": "Look up one order by its id.",
            "parameters": {"type": "object", "properties": {}}, "kind": "verbatim",
            "citation": "support_agent.py:22",
            "backend": {"kind": "lookup", "state_key": "orders", "match_fields": ["order_id"],
                        "citation": "support_agent.py:56", "reason": "pure dict lookup"}}
    result = verify(minimal_extraction(tools=[tool]), HANDROLLED)
    assert result.confidence["backend.py"] == MEDIUM
    assert any("NOT machine-verified" in f.reason for f in result.flags_for("backend.py"))


def test_a_mechanical_claim_without_a_readable_citation_becomes_a_stub():
    tool = {"name": "lookup_order", "description": "Look up one order by its id.",
            "parameters": {"type": "object", "properties": {}}, "kind": "verbatim",
            "citation": "support_agent.py:22",
            "backend": {"kind": "lookup", "state_key": "orders", "citation": "ghost.py:4"}}
    result = verify(minimal_extraction(tools=[tool]), HANDROLLED)
    assert result.data["tools"][0]["backend"]["kind"] == "unclear"
    assert result.confidence["backend.py"] == LOW


def test_a_case_that_cites_nothing_readable_is_not_grounded():
    case = {"id": "c1", "description": "", "user_messages": ["hi"], "initial_state": {},
            "expected_tool_calls": [], "final_message": "hi", "checks": [],
            "citation": "ghost.md:2", "note": ""}
    result = verify(minimal_extraction(cases=[case]), HANDROLLED)
    assert result.data["cases"][0]["verified"] is False
    assert result.confidence["cases/cases.json"] == LOW


def test_grounded_cases_are_never_better_than_medium():
    case = {"id": "c1", "description": "", "user_messages": ["hi"], "initial_state": {},
            "expected_tool_calls": [], "final_message": "hi", "checks": [],
            "citation": "README.md:9", "note": ""}
    result = verify(minimal_extraction(cases=[case]), HANDROLLED)
    assert result.confidence["cases/cases.json"] == MEDIUM


# ---------------------------------------------------------------------------
# Recording + cost integration (no network: a canned provider)
# ---------------------------------------------------------------------------


class FakeProvider(Provider):
    name = "openai"

    def __init__(self, replies: list[str], error: Exception | None = None) -> None:
        self.replies = replies
        self.error = error
        self.calls: list[dict] = []

    def call(self, endpoint, request, seed_key, sim_context=None):
        self.calls.append(request)
        if self.error:
            raise self.error
        return chat_response(self.replies[min(len(self.calls), len(self.replies)) - 1])


def test_extraction_is_recorded_in_a_shape_upshift_cost_can_price(tmp_path):
    from upshift.pricing import run_cost

    _, inv = inventory_of(HANDROLLED, tmp_path / "src")
    provider = FakeProvider([json.dumps({"agent_name": "x"}), json.dumps(minimal_extraction())])
    runs_root = tmp_path / "runs"
    extractor = RecordingExtractor(
        provider, model="gpt-5.5", run_id="adapt-orderly", runs_root=runs_root,
        max_cost_usd=None, source=str(HANDROLLED), commit="abc123",
    )
    extractor.start(evidence_tokens=inv.evidence_tokens, out_dir=str(tmp_path / "out"))
    result = extract(inv, call_model=extractor, model="gpt-5.5")
    extractor.finalize(result)

    run_directory = runs_root / "adapt-orderly"
    manifest = json.loads((run_directory / "manifest.json").read_text())
    assert manifest["provider"] == "openai"
    assert manifest["agent"]["model_requested"] == "gpt-5.5"
    assert manifest["adapt"]["commit"] == "abc123"

    reps = sorted((run_directory / "cases" / "extraction").glob("rep_*.json"))
    assert len(reps) == 2, "both attempts, including the rejected one, are on disk"
    first = json.loads(reps[0].read_text())
    assert first["passed"] is False
    assert first["check_results"][0]["check"]["type"] == "extraction_schema_valid"
    assert first["api_calls"][0]["request"]["model"] == "gpt-5.5"
    assert first["api_calls"][0]["response"]["model"] == "gpt-5.5-2026-01-01"
    assert json.loads(reps[1].read_text())["passed"] is True

    priced = run_cost(run_directory)
    assert priced["input_tokens"] == 2000
    assert priced["output_tokens"] == 400
    assert priced["cached_input_tokens"] == 200
    assert priced["usd"] == pytest.approx(
        (1800 * 5.0 + 200 * 0.5 + 400 * 30.0) / 1_000_000
    )
    assert (run_directory / "summary.json").is_file()


def test_the_cost_guard_aborts_before_the_call_is_made(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path / "src")
    provider = FakeProvider([json.dumps(minimal_extraction())])
    extractor = RecordingExtractor(
        provider, model="gpt-5.5", run_id="adapt-broke", runs_root=tmp_path / "runs",
        max_cost_usd=0.0001,
    )
    extractor.start(evidence_tokens=inv.evidence_tokens, out_dir=str(tmp_path / "out"))
    with pytest.raises(AdaptAborted) as excinfo:
        extract(inv, call_model=extractor, model="gpt-5.5")
    assert "--max-cost-usd" in excinfo.value.message
    assert provider.calls == [], "nothing may be sent once the budget is blown"


def test_a_provider_error_is_recorded_before_it_propagates(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path / "src")
    provider = FakeProvider([], error=ProviderAPIError("boom", status_code=500))
    runs_root = tmp_path / "runs"
    extractor = RecordingExtractor(
        provider, model="gpt-5.5", run_id="adapt-err", runs_root=runs_root, max_cost_usd=None
    )
    extractor.start(evidence_tokens=1, out_dir=str(tmp_path / "out"))
    with pytest.raises(ProviderAPIError):
        extract(inv, call_model=extractor, model="gpt-5.5")
    extractor.finalize(ExtractionResult(data={}))
    rep = json.loads((runs_root / "adapt-err" / "cases" / "extraction" / "rep_01.json").read_text())
    assert rep["api_error"]["message"] == "boom"
    assert rep["api_calls"][0]["response"] is None
