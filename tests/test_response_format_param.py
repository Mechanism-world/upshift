"""`response_format` is the agent's, not upshift's — and a nullable field keeps its null.

rescue-ops `ghisdk-051` (getzep/graphiti) was a structured-output incident: the 400 was
`Invalid schema for response_format 'ExtractedEdges'`. `upshift adapt` put `response_format`
in `BLOCKED_PARAMS`, so it deleted the entire subject of the failure and produced an agent
that asked for no structured output at all — a run against which would have reported the
incident as fixed. `capture/fidelity.py` was written to at least NAME that loss; carrying the
parameter is the actual repair.

Carrying it means translating it, because the two OpenAI endpoints spell it differently:
`response_format` on chat/completions, `text.format` on `/v1/responses`, with the json_schema
object flattened one level. OpenAI's migration guide states it directly — "Instead of
`response_format`, use `text.format` in Responses"
(https://developers.openai.com/api/docs/guides/migrate-to-responses). Anthropic's Messages
API has no equivalent, so it is DROPPED there with a reason in the record, never silently.

The second half is `ghisdk-052`: a `str | None` parameter reached the model as
`{"type": "string"}` with no null option, the model had no way to say "nothing", and sent `""`
instead. Wherever a tool schema declares optionality-with-null, upshift canonicalises it to
`anyOf` with a null branch rather than dropping the branch.
"""

from __future__ import annotations

import json
from pathlib import Path

from upshift.adapt.extract import DICT_PARAMS
from upshift.adapt.generate import BLOCKED_PARAMS, build_params, build_tools
from upshift.jsonschema import canonicalise_nullable

#: The exact shape graphiti sent (ops/cases/ghisdk-051/CASE.md), reduced to two properties.
GRAPHITI_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "ExtractedEdges",
        "schema": {
            "type": "object",
            "properties": {
                "edges": {"type": "array", "items": {"type": "string"}},
                "valid_at": {"type": "string"},
            },
            "required": ["edges"],
        },
    },
}


# ---------------------------------------------------------------------------
# adapt carries it
# ---------------------------------------------------------------------------


def test_response_format_is_no_longer_blocked() -> None:
    assert "response_format" not in BLOCKED_PARAMS
    assert "response_format" in DICT_PARAMS
    assert "reasoning" in DICT_PARAMS


def test_build_params_round_trips_the_graphiti_shape(tmp_path: Path) -> None:
    notes: list[str] = []
    params = build_params(
        {"response_format": GRAPHITI_RESPONSE_FORMAT, "temperature": 0}, "chat_completions",
        notes,
    )
    assert params["response_format"] == GRAPHITI_RESPONSE_FORMAT
    assert not any("response_format" in note for note in notes), (
        "carrying it must not also report it as dropped"
    )


def test_it_survives_serialisation_into_agent_json(tmp_path: Path) -> None:
    """agent.json is JSON on disk; a param that cannot round-trip through it is not carried."""
    params = build_params({"response_format": GRAPHITI_RESPONSE_FORMAT}, "chat_completions", [])
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"params": params}, indent=1))
    assert json.loads(path.read_text())["params"]["response_format"] == GRAPHITI_RESPONSE_FORMAT


def test_a_still_blocked_param_is_still_dropped_with_a_note() -> None:
    """The blocklist is narrowed, not abandoned: `messages` is still upshift's."""
    notes: list[str] = []
    params = build_params({"messages": [{"role": "user", "content": "x"}]}, "chat_completions",
                          notes)
    assert params == {}
    assert any("messages" in note for note in notes)


# ---------------------------------------------------------------------------
# ghisdk-052: `str | None` keeps its null
# ---------------------------------------------------------------------------


def test_openapi_nullable_becomes_an_any_of_with_null() -> None:
    assert canonicalise_nullable({"type": "string", "nullable": True}) == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }


def test_a_type_list_containing_null_becomes_an_any_of() -> None:
    assert canonicalise_nullable({"type": ["string", "null"]}) == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }


def test_a_schema_that_already_says_any_of_null_is_untouched() -> None:
    already = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert canonicalise_nullable(already) == already


def test_a_plain_string_is_untouched() -> None:
    assert canonicalise_nullable({"type": "string"}) == {"type": "string"}


def test_it_recurses_into_properties_items_and_defs() -> None:
    schema = {
        "type": "object",
        "properties": {"key": {"type": "string", "nullable": True}},
        "$defs": {"Note": {"type": ["string", "null"]}},
    }
    out = canonicalise_nullable(schema)
    assert out["properties"]["key"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert out["$defs"]["Note"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert canonicalise_nullable({"type": "array", "items": {"type": ["integer", "null"]}}) == {
        "type": "array",
        "items": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    }


def test_the_input_is_not_mutated() -> None:
    schema = {"type": "string", "nullable": True}
    canonicalise_nullable(schema)
    assert schema == {"type": "string", "nullable": True}


def test_adapt_writes_the_null_branch_into_tools_json() -> None:
    """The end of the ghisdk-052 path: `recall_from_scratchpad(key: str | None = None)`
    reaches the model able to express "nothing"."""
    tools, _ = build_tools(
        {
            "tools": [
                {
                    "name": "recall_from_scratchpad",
                    "description": "Read the scratchpad.",
                    "parameters": {
                        "type": "object",
                        "properties": {"key": {"type": "string", "nullable": True}},
                        "required": [],
                    },
                }
            ]
        }
    )
    key = tools[0]["function"]["parameters"]["properties"]["key"]
    assert key == {"anyOf": [{"type": "string"}, {"type": "null"}]}
