"""Capture fidelity: what crossed the wire, what reached the agent directory, what did not.

The audit exists because of one measured failure. `upshift adapt` put `response_format` in
`BLOCKED_PARAMS` and dropped it, silently, on a case whose whole subject was a structured-output
regression (rescue-ops `ops/cases/ghisdk-051/CASE.md:308`, $0.3060 spent to learn it). A field
that is dropped without a word reads downstream as a field that was never there — and the
verdict then says `SAFE` about an agent nobody has.

So: everything on a recorded request that the adapter does not carry is reported, by name, with
a count, in `ADAPT_EDITS.md` and in machine-readable `unsupported_fields.json`. The design is
denylist-free on purpose — the next field a provider invents shows up as a finding on its first
capture instead of as silence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift.capture import fidelity
from upshift.capture.adapt import adapt_from_capture
from upshift.capture.record import CaptureStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "capture_fixtures" / "cookbook_sms"


def _kinds(findings: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for finding in findings:
        out.setdefault(finding["kind"], []).append(finding["field"])
    return out


def _capture_with(out_dir: Path, *, body_extra: dict, headers: dict | None = None,
                  tools: list | None = None, response: dict | None = None,
                  status: int = 200, streamed: bool = False, upstream: str = "x") -> Path:
    store = CaptureStore(out_dir, listen="127.0.0.1:0", upstream=upstream, mode="forward")
    body = {
        "model": "claude-fable-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hello"}],
        "system": "Be brief.",
        "tools": tools if tools is not None else [
            {"name": "echo", "description": "echo", "input_schema": {"type": "object"}}
        ],
        **body_extra,
    }
    store.add(
        headers=headers or {"user-agent": "anthropic-sdk-python/1.2.0"},
        body=body,
        raw_body_bytes=300,
        path="/v1/messages",
        status=status,
        response_body=response
        or {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-fable-5",
            "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"},
        events=None,
        streamed=streamed,
        latency_s=0.1,
    )
    store.close()
    return out_dir


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_the_kind_vocabulary_is_closed_and_every_finding_uses_it(tmp_path: Path) -> None:
    capture = _capture_with(tmp_path / "cap", body_extra={"response_format": {"type": "json"}})
    from upshift.capture.record import load_capture

    index, conversations = load_capture(capture)
    findings = fidelity.audit(index, conversations)
    assert findings
    for finding in findings:
        assert finding["kind"] in fidelity.KINDS
        assert set(finding) == {"field", "where", "kind", "count", "detail", "sample"}
        assert finding["detail"]


def test_response_format_is_carried_rather_than_reported_as_a_loss(tmp_path: Path) -> None:
    """The ghisdk-051 regression, pinned — at its new resting place.

    This test used to assert that the dropped `response_format` at least got NAMED in
    `unsupported_fields.json`, which was the best the adapter could do while the parameter was
    in `BLOCKED_PARAMS`. It is carried now, so the honest assertion is the stronger one: the
    contract reaches agent.json, and the module that exists to name losses names no loss.
    """
    out = tmp_path / "agent"
    result = adapt_from_capture(
        _capture_with(tmp_path / "cap", body_extra={"response_format": {"type": "json_object"}}),
        out,
    )
    assert "response_format" not in {f["field"] for f in result.unsupported_fields}
    params = json.loads((out / "agent.json").read_text())["params"]
    assert params["response_format"] == {"type": "json_object"}
    written = json.loads((out / "unsupported_fields.json").read_text())
    assert {f["field"] for f in written} == {f["field"] for f in result.unsupported_fields}


def test_an_unknown_future_field_is_reported_without_anyone_adding_it_to_a_list(
    tmp_path: Path,
) -> None:
    """Denylist-free: the audit reports what it does NOT carry, not what it knows about."""
    result = adapt_from_capture(
        _capture_with(tmp_path / "cap", body_extra={"quantum_effort": {"level": 11}}),
        tmp_path / "agent",
    )
    finding = next(f for f in result.unsupported_fields if f["field"] == "quantum_effort")
    assert finding["kind"] == "dropped_param"
    assert "not carry it" in finding["detail"]


def test_a_server_tool_is_reported_as_one_not_as_a_custom_tool(tmp_path: Path) -> None:
    """A-075 §6.3(b): dropping `type` turns a server tool into an empty-schema custom tool."""
    result = adapt_from_capture(
        _capture_with(
            tmp_path / "cap",
            body_extra={},
            tools=[{"type": "computer_20241022", "name": "computer", "display_width_px": 1024}],
        ),
        tmp_path / "agent",
    )
    kinds = _kinds(result.unsupported_fields)
    assert "tools[computer].type" in kinds["server_tool"]
    assert "tools[computer].display_width_px" in kinds["server_tool"]
    assert any("A-075" in f["detail"] for f in result.unsupported_fields)


def test_streaming_boundaries_are_named_as_the_thing_that_is_lost(tmp_path: Path) -> None:
    result = adapt_from_capture(
        _capture_with(tmp_path / "cap", body_extra={"stream": True}, streamed=True),
        tmp_path / "agent",
    )
    finding = next(f for f in result.unsupported_fields if f["kind"] == "streaming")
    assert "BOUNDARIES are not" in finding["detail"]
    assert "A-061" in finding["detail"]  # the reasoning-part assembly defects


def test_a_beta_header_is_a_finding_because_upshift_sends_none(tmp_path: Path) -> None:
    result = adapt_from_capture(
        _capture_with(
            tmp_path / "cap",
            body_extra={},
            headers={"user-agent": "opencode/1.0",
                     "anthropic-beta": "thinking-binding-controls-2026-08-01"},
        ),
        tmp_path / "agent",
    )
    finding = next(f for f in result.unsupported_fields if f["kind"] == "beta_header")
    assert finding["sample"] == "thinking-binding-controls-2026-08-01"


def test_a_response_block_the_adapter_cannot_represent_is_listed(tmp_path: Path) -> None:
    result = adapt_from_capture(
        _capture_with(
            tmp_path / "cap",
            body_extra={},
            response={"id": "m", "type": "message", "role": "assistant", "model": "claude-fable-5",
                      "content": [{"type": "text", "text": "hi"},
                                  {"type": "server_tool_use", "id": "s1", "name": "web_search"}],
                      "stop_reason": "end_turn"},
        ),
        tmp_path / "agent",
    )
    kinds = _kinds(result.unsupported_fields)
    assert "content[type=server_tool_use]" in kinds["response_block"]


def test_thinking_blocks_are_not_listed_because_dropping_them_is_the_documented_design(
    tmp_path: Path,
) -> None:
    result = adapt_from_capture(
        _capture_with(
            tmp_path / "cap",
            body_extra={},
            response={"id": "m", "type": "message", "role": "assistant", "model": "claude-fable-5",
                      "content": [{"type": "thinking", "thinking": "…", "signature": "sig"},
                                  {"type": "text", "text": "hi"}],
                      "stop_reason": "end_turn"},
        ),
        tmp_path / "agent",
    )
    fields = {f["field"] for f in result.unsupported_fields}
    assert "content[type=thinking]" not in fields
    # It is still reported — as structural deviation 1, where the reasoning lives.
    assert "Thinking blocks are not replayed" in (tmp_path / "agent" / "ADAPT_EDITS.md").read_text()


def test_a_gateway_upstream_is_reported_as_a_different_contract(tmp_path: Path) -> None:
    """A-017: `only \\`auto\\` is supported for tool_choice` is a gateway's sentence."""
    result = adapt_from_capture(
        _capture_with(tmp_path / "cap", body_extra={}, upstream="https://gateway.internal"),
        tmp_path / "agent",
    )
    finding = next(f for f in result.unsupported_fields if f["kind"] == "gateway")
    assert "gateway.internal" in finding["sample"]
    assert "A-017" in finding["detail"]


def test_an_error_response_in_the_capture_is_flagged_as_the_incident(tmp_path: Path) -> None:
    result = adapt_from_capture(
        _capture_with(
            tmp_path / "cap", body_extra={}, status=400,
            response={"type": "error", "error": {"type": "invalid_request_error",
                                                 "message": "boom"},
                      "content": []},
        ),
        tmp_path / "agent",
    )
    finding = next(f for f in result.unsupported_fields if f["kind"] == "error_response")
    assert finding["field"] == "status=400"
    assert "Read it as the incident" in finding["detail"]


def test_the_unrecorded_paths_are_listed_on_every_capture(tmp_path: Path) -> None:
    """An absence cannot be detected from the recording, so it is stated unconditionally
    (A-058, A-059: the Batches endpoint is relayed, never recorded)."""
    result = adapt_from_capture(_capture_with(tmp_path / "cap", body_extra={}), tmp_path / "agent")
    finding = next(f for f in result.unsupported_fields if f["kind"] == "unrecorded_path")
    assert "batches" in finding["field"]
    assert "A-058" in finding["detail"]


# ---------------------------------------------------------------------------
# The real fixture: the honest baseline
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("fidelity") / "agent"
    adapt_from_capture(FIXTURE, out)
    return out


def test_a_clean_capture_still_reports_the_boundary_it_cannot_see(real: Path) -> None:
    """Nothing was dropped from these bytes — and the unrecordable paths are still stated."""
    findings = json.loads((real / "unsupported_fields.json").read_text())
    kinds = _kinds(findings)
    assert set(kinds) == {"unrecorded_path"}


def test_the_markdown_table_carries_the_same_rows_as_the_json(real: Path) -> None:
    findings = json.loads((real / "unsupported_fields.json").read_text())
    edits = (real / "ADAPT_EDITS.md").read_text()
    assert "Fields the adapter cannot carry" in edits
    for finding in findings:
        assert f"`{finding['field']}`" in edits
    # The machine-readable copy is embedded too, so one file answers both audiences.
    assert json.dumps(findings, indent=1, sort_keys=True) in edits
