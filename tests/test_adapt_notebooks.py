"""`upshift adapt` on an agent that lives in a Jupyter notebook.

Motivation, from the wild: anthropics/claude-cookbooks keeps its agents in `.ipynb` files.
Before notebooks were readable, adapt's inventory walked such a repo, saw only the handful of
`.py` files around the notebooks, and produced an empty adapter with the wrong model.

A notebook is never shown to the pipeline as JSON. `inventory.read_text` renders it into a
synthetic text document — cells in order under `# --- cell N (type) ---` markers, code cells
verbatim, markdown commented — and every stage that re-reads a cited file goes through that
same function, so a `path:line` citation, the evidence the model saw, and the verbatim gate
all mean the same document.

Everything here is offline: the extraction engine's `call_model` is scripted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift.adapt.extract import extract, validate_extraction
from upshift.adapt.generate import generate
from upshift.adapt.inventory import (
    read_text,
    readable_source,
    render_evidence,
    render_notebook,
    resolve_source,
    take_inventory,
)
from upshift.adapt.verify import verify

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "tests" / "adapt_fixtures" / "notebook_agent"
NOTEBOOK_FILE = NOTEBOOK / "agent.ipynb"
HANDROLLED = ROOT / "tests" / "adapt_fixtures" / "handrolled"

RENDERED = read_text(NOTEBOOK_FILE) or ""


def line_of(needle: str) -> int:
    """1-based line of `needle` in the RENDERED notebook — the only line numbering that any
    citation about a notebook may use."""
    for index, line in enumerate(RENDERED.splitlines(), 1):
        if needle in line:
            return index
    raise AssertionError(f"{needle!r} is not in the rendered notebook")


def cite(needle: str) -> str:
    return f"agent.ipynb:{line_of(needle)}"


# ---------------------------------------------------------------------------
# The rendering rule
# ---------------------------------------------------------------------------


def test_the_notebook_is_readable_source():
    assert readable_source(NOTEBOOK_FILE) is True


def test_read_text_renders_cells_with_markers_and_drops_outputs():
    lines = RENDERED.splitlines()
    assert lines[0] == "# --- cell 0 (markdown) ---"
    assert "# --- cell 1 (code) ---" in lines
    assert "# --- cell 2 (code) ---" in lines
    assert "# --- cell 3 (code) ---" in lines
    # Code cells verbatim; markdown commented so the document still reads as Python.
    assert "    return client.messages.create(" in lines
    assert '#     ask("What is on the calendar for Friday?")' in lines
    # Raw JSON never reaches the evidence, and neither does anything from `outputs`.
    assert '"cell_type"' not in RENDERED
    assert "execution_count" not in RENDERED
    assert "OUTPUT NOISE" not in RENDERED


def test_the_rendering_is_deterministic_and_parses_as_python():
    import ast

    assert read_text(NOTEBOOK_FILE) == RENDERED
    ast.parse(RENDERED)  # which is what lets the AST pass run on a notebook


def test_a_markdown_only_notebook_still_renders():
    raw = json.dumps({"cells": [{"cell_type": "markdown", "source": ["hello\n", "world"]}]})
    assert render_notebook(raw) == "# --- cell 0 (markdown) ---\n# hello\n# world\n"


def test_a_notebook_with_no_cells_renders_empty():
    assert render_notebook(json.dumps({"cells": []})) == ""


# ---------------------------------------------------------------------------
# Stage 1: the notebook as evidence
# ---------------------------------------------------------------------------


def inventory_of(path: Path, tmp_path: Path, **kwargs):
    repo = resolve_source(str(path), tmp_path)
    return repo, take_inventory(repo, **kwargs)


def test_the_notebook_ranks_first_and_carries_the_anthropic_signals(tmp_path):
    _, inv = inventory_of(NOTEBOOK, tmp_path)
    assert inv.files[0].path == "agent.ipynb"
    reasons = " ".join(inv.files[0].reasons)
    for expected in ("messages.create call", "anthropic import",
                     "Anthropic client construction", "Anthropic tool schema literal",
                     "tools= kwarg", "tool_choice"):
        assert expected in reasons, expected


def test_the_ast_runs_on_the_rendered_notebook(tmp_path):
    _, inv = inventory_of(NOTEBOOK, tmp_path)
    site = next(s for s in inv.call_sites if s.callee.endswith("messages.create"))
    assert site.how == "ast"
    assert site.path == "agent.ipynb"
    assert site.kwargs["model"] == "'claude-fable-5'"
    assert site.kwargs["tools"] == "TOOLS"
    assert site.kwargs["tool_choice"] == "{'type': 'any'}"
    # The citation points at a real line of the RENDERED document.
    assert "messages.create" in RENDERED.splitlines()[site.line - 1]
    assert {p.name for p in inv.prompt_constants} == {"SYSTEM_PROMPT"}


def test_an_unparseable_notebook_falls_back_to_the_regex_scan(tmp_path):
    """Magics and shell escapes are ordinary in notebooks and are not Python."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "magic.ipynb").write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "source": ["!pip install anthropic\n",
                                                     "%load_ext autoreload\n"]},
                    {"cell_type": "code", "source": [
                        "import anthropic\n",
                        "r = client.messages.create(model='claude-fable-5', tools=TOOLS)\n",
                    ]},
                ]
            }
        )
    )
    _, inv = inventory_of(repo_dir, tmp_path / "work")
    assert [f.path for f in inv.files] == ["magic.ipynb"]
    site = next(s for s in inv.call_sites if "messages.create" in s.callee)
    assert site.how == "regex"
    assert "messages.create" in (read_text(repo_dir / "magic.ipynb") or "").splitlines()[
        site.line - 1
    ]


def test_a_malformed_notebook_is_skipped_without_crashing(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "broken.ipynb").write_text('{"cells": [ this is not json')
    (repo_dir / "notacell.ipynb").write_text('{"metadata": {}}')
    (repo_dir / "agent.py").write_text(
        "from anthropic import Anthropic\n"
        'SYSTEM_PROMPT = "You are a helpful assistant that books rooms."\n'
        'r = client.messages.create(model="claude-fable-5", tools=TOOLS)\n'
    )
    assert read_text(repo_dir / "broken.ipynb") is None
    assert read_text(repo_dir / "notacell.ipynb") is None
    _, inv = inventory_of(repo_dir, tmp_path / "work")
    assert [f.path for f in inv.files] == ["agent.py"]
    assert inv.scanned_files == 1  # the two unreadable notebooks were skipped, not counted


def test_the_evidence_bundle_cites_rendered_line_numbers(tmp_path):
    _, inv = inventory_of(NOTEBOOK, tmp_path)
    evidence = render_evidence(inv)
    assert "===== FILE agent.ipynb lines 1-" in evidence
    numbered = f"{line_of('client.messages.create'):>5}| "
    assert numbered in evidence
    assert "# --- cell 3 (code) ---" in evidence


def test_a_non_notebook_file_is_read_byte_for_byte():
    """The notebook path must not touch anything else."""
    for name in ("support_agent.py", "README.md"):
        path = HANDROLLED / name
        assert read_text(path) == path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stages 2-4: a scripted extraction citing the rendered document
# ---------------------------------------------------------------------------


def _extraction() -> dict:
    return {
        "agent_name": "bookbot",
        "endpoint": {"value": "messages", "citation": cite("client.messages.create"),
                     "status": "found", "note": "notebook cell 3"},
        "model": {"value": "claude-fable-5", "citation": cite('model="claude-fable-5"'),
                  "status": "found", "note": ""},
        "params": {
            "value": {"max_tokens": 1024, "tool_choice": {"type": "any"}},
            "citation": cite("max_tokens=1024"),
            "status": "found",
            "note": "",
        },
        "max_turns": {"value": None, "citation": "", "status": "undetermined", "note": ""},
        "system_prompt": {
            "status": "found",
            "note": "one implicitly concatenated string in cell 1",
            "chunks": [
                {"text": "You are Bookbot, a scheduling assistant for the Ledger team.",
                 "kind": "verbatim", "citation": cite("You are Bookbot"), "note": ""},
                {"text": "Search the calendar before you answer, and never invent an event.",
                 "kind": "verbatim", "citation": cite("Search the calendar"), "note": ""},
            ],
        },
        "tools": [
            {
                "name": "search_calendar",
                "description": "Search the team calendar for matching events.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string",
                                             "description": "What to search for."}},
                    "required": ["query"],
                },
                "kind": "verbatim",
                "citation": cite('"name": "search_calendar"'),
                "backend": {"kind": "lookup", "state_key": "events", "id_field": "",
                            "id_prefix": "", "match_fields": ["query"], "text_field": "",
                            "citation": cite("input_schema"),
                            "reason": "pure filter over the calendar index"},
            },
        ],
        "cases": [
            {
                "id": "friday_lookup",
                "description": "The usage example in the notebook's markdown cell.",
                "user_messages": ["What is on the calendar for Friday?"],
                "initial_state": {"events": [
                    {"query": "Friday", "title": "Ledger standup"},
                ]},
                "expected_tool_calls": [
                    {"name": "search_calendar", "arguments": {"query": "Friday"}},
                ],
                "final_message": "Friday has the Ledger standup.",
                "checks": [{"type": "response_contains", "text": "standup"}],
                "citation": cite("ask(\"What is on the calendar for Friday?\")"),
                "note": "",
            },
        ],
        "undetermined": [],
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


def scripted(reply: str):
    def call_model(request: dict) -> dict:
        return chat_response(reply)

    return call_model


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("adapt-notebook")
    repo = resolve_source(str(NOTEBOOK), tmp)
    inventory = take_inventory(repo)
    extraction = extract(
        inventory, call_model=scripted(json.dumps(_extraction())), model="gpt-5.5",
        second_round=False,
    )
    assert extraction.ok, extraction.errors
    verification = verify(extraction.data, repo.root)
    out_dir = tmp / "bookbot_agent"
    result = generate(verification, out_dir, origin=str(NOTEBOOK), commit="abc1234")
    return verification, result, out_dir


def test_the_scripted_extraction_is_schema_valid():
    assert validate_extraction(_extraction()) == []


def test_citations_into_the_rendered_notebook_pass_the_verbatim_gate(generated):
    verification, _, _ = generated
    assert verification.data["endpoint"]["verified"] is True
    assert "messages.create" in verification.data["endpoint"]["verify_detail"]
    for chunk in verification.data["system_prompt"]["chunks"]:
        assert chunk["verify"] == "at_citation", chunk
        assert chunk["kind"] == "verbatim"
        assert chunk["omitted"] is False
    tool = verification.data["tools"][0]
    assert tool["verify"] == {"name": "at_citation", "description": "at_citation"}
    assert verification.dropped_params == []


def test_the_gate_reads_the_rendered_notebook_not_the_json():
    """Text that exists only in the .ipynb JSON — a cell output, an nbformat key — is not
    evidence, because it was never in the document the model was shown."""
    data = _extraction()
    data["system_prompt"]["chunks"] = [
        {"text": "OUTPUT NOISE, NOT SOURCE", "kind": "verbatim",
         "citation": "agent.ipynb:1", "note": ""},
    ]
    verification = verify(data, NOTEBOOK)
    chunk = verification.data["system_prompt"]["chunks"][0]
    assert chunk["verify"] == "absent"
    assert chunk["omitted"] is True
    # And the raw JSON really does contain it, so this is the renderer's doing.
    assert "OUTPUT NOISE, NOT SOURCE" in NOTEBOOK_FILE.read_text()


def test_generate_writes_the_notebooks_tools_and_endpoint(generated):
    _, result, out_dir = generated
    config = json.loads((out_dir / "agent.json").read_text())
    assert config["endpoint"] == "messages"
    assert config["model"] == "claude-fable-5"
    assert config["params"]["tool_choice"] == {"type": "any"}
    assert config["params"]["max_tokens"] == 1024

    tools = json.loads((out_dir / "tools.json").read_text())
    assert [t["function"]["name"] for t in tools] == ["search_calendar"]
    assert tools[0]["function"]["parameters"]["required"] == ["query"]
    assert "input_schema" not in json.dumps(tools)

    assert (out_dir / "system_prompt.txt").read_text() == (
        "You are Bookbot, a scheduling assistant for the Ledger team.\n"
        "Search the calendar before you answer, and never invent an event.\n"
    )
    assert "cases/cases.json" in result.files


def test_provenance_cites_the_notebook_by_rendered_line(generated):
    _, _, out_dir = generated
    provenance = json.dumps(json.loads((out_dir / "PROVENANCE.json").read_text()))
    assert "agent.ipynb:" in provenance
