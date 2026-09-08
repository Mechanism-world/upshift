"""Rescue regression 7 — a captured field the adapter cannot carry is reported, never lost.

maintenance coverage derived from the Anthropic rescue track's capture cases (36
UNSUPPORTED_FRAMEWORK cases routed into capture mode; PRODUCT_RELIABILITY_UPGRADE.md finding
#5) — not independent evidence of general repair capability.

`tools.json` has three slots: name, description, parameters. A recorded server tool carries
more — `{"type": "computer_20241022", "display_width_px": 1024}` has no `input_schema` at
all — and silently rewriting it as a plain custom tool with an empty schema replays a
*different agent* than the one captured. Every uncarried field has to reach the operator in
ADAPT_EDITS.md, per tool, by name and value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift.capture.adapt import adapt_from_capture
from upshift.capture.record import CaptureStore

pytestmark = [pytest.mark.recorded_fixture]

SERVER_TOOL = {
    "type": "computer_20241022",
    "name": "computer",
    "display_width_px": 1024,
    "input_schema": {"type": "object", "properties": {}},
}


def _capture_with_server_tool(out_dir: Path) -> Path:
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(
        headers={"user-agent": "framework/1.0"},
        body={
            "model": "claude-fable-5",
            "max_tokens": 512,
            "system": "You drive a screen.",
            "messages": [{"role": "user", "content": "click the button"}],
            "tools": [SERVER_TOOL],
        },
        raw_body_bytes=400,
        path="/v1/messages",
        status=200,
        response_body={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-fable-5",
            "content": [{"type": "text", "text": "Clicked."}],
            "stop_reason": "end_turn",
        },
        events=None,
        streamed=False,
        latency_s=0.1,
    )
    store.close()
    return out_dir


def test_the_uncarried_fields_are_named_with_their_values(tmp_path):
    result = adapt_from_capture(_capture_with_server_tool(tmp_path / "cap"), tmp_path / "agent")

    notes = " ".join(result.notes)
    assert "display_width_px" in notes, f"a recorded tool field vanished; notes were {notes!r}"
    assert "1024" in notes
    assert "computer_20241022" in notes
    assert "computer" in notes


def test_the_report_tells_the_operator_it_is_not_the_recorded_tool(tmp_path):
    adapt_from_capture(_capture_with_server_tool(tmp_path / "cap"), tmp_path / "agent")

    edits = (tmp_path / "agent" / "ADAPT_EDITS.md").read_text()
    assert "display_width_px" in edits
    assert "it is not the recorded tool" in edits
    # The specific reason a server tool cannot be reconstructed at all.
    assert "supplied by the API" in edits


def test_the_generated_tools_json_does_not_pretend_to_carry_the_field(tmp_path):
    adapt_from_capture(_capture_with_server_tool(tmp_path / "cap"), tmp_path / "agent")

    tools = json.loads((tmp_path / "agent" / "tools.json").read_text())
    assert "display_width_px" not in json.dumps(tools), (
        "the field must be dropped honestly and reported, not smuggled into the schema"
    )
    assert tools[0]["function"]["name"] == "computer"


def test_a_plain_tool_produces_no_uncarried_field_note(tmp_path):
    """The negative half: the note must mean something, so an ordinary tool gets none."""
    store = CaptureStore(tmp_path / "plain", listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(
        headers={"user-agent": "framework/1.0"},
        body={
            "model": "claude-fable-5",
            "max_tokens": 512,
            "system": "You look things up.",
            "messages": [{"role": "user", "content": "status?"}],
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look something up.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        },
        raw_body_bytes=400,
        path="/v1/messages",
        status=200,
        response_body={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-fable-5",
            "content": [{"type": "text", "text": "Fine."}],
            "stop_reason": "end_turn",
        },
        events=None,
        streamed=False,
        latency_s=0.1,
    )
    store.close()

    result = adapt_from_capture(tmp_path / "plain", tmp_path / "agent")
    assert not [n for n in result.notes if "cannot carry" in n]
