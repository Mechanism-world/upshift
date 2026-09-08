"""A strict-schema 400 gets a repair candidate, and the candidate is disclosed.

Two rescue-ops cases converge here:

* `ghisdk-051` (getzep/graphiti): a real, three-week silent outage whose 400 read
  `Invalid schema for response_format 'ExtractedEdges': In context=(), 'additionalProperties'
  is required to be supplied and to be false.` upshift had no signature for it and no
  candidate for it, so the loop had nothing to try.
* `p2-001` (openchamber): endpoint routing alone took the suite from 0/13 to 0/13; routing
  PLUS a two-line tool-schema edit took it to 12/13. "no repair candidate edits a tool or
  response JSON Schema" was written down as the product's biggest gap after that case.

The repair applies OpenAI's documented strict-subset rules
(https://platform.openai.com/docs/guides/structured-outputs) to ONE named schema, and it is
disclosed, because rule 3 changes the agent: a property that used to be omittable is now
required and explicitly null, and downstream code has to accept that.
"""

from __future__ import annotations

import json
from pathlib import Path

from upshift import differ
from upshift.jsonschema import is_strict, make_strict, strict_violations
from upshift.repair.playbook import (
    RANK_BEHAVIOURAL,
    RANK_CAPABILITY,
    RANK_TRANSPORT,
    disclosures_for,
    generate_candidates,
    rank_for,
)

#: The 400 graphiti's users saw, character for character (ops/cases/ghisdk-051/CASE.md).
GRAPHITI_400 = {
    "status_code": 400,
    "type": "api_status_error",
    "message": (
        "Invalid schema for response_format 'ExtractedEdges': In context=(), "
        "'additionalProperties' is required to be supplied and to be false."
    ),
}

LOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "edges": {"type": "array", "items": {"type": "string"}},
        "valid_at": {"type": "string"},
    },
    "required": ["edges"],
}


def _agent(directory: Path, *, params: dict | None = None, tools: list | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.json").write_text(
        json.dumps(
            {
                "name": "a",
                "endpoint": "chat_completions",
                "model": "m",
                "params": params or {},
                "system_prompt_file": "prompt.txt",
                "tools_file": "tools.json",
                "max_turns": 4,
            },
            indent=2,
        )
    )
    (directory / "prompt.txt").write_text("BASE\n")
    (directory / "tools.json").write_text(json.dumps(tools or [], indent=2))
    return directory


def _response_format(schema: dict) -> dict:
    return {"type": "json_schema", "json_schema": {"name": "ExtractedEdges", "schema": schema}}


def _tool(name: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "d", "parameters": parameters},
    }


# ---------------------------------------------------------------------------
# The signature
# ---------------------------------------------------------------------------


def test_the_graphiti_400_gets_its_own_signature() -> None:
    assert differ._api_error_signature(GRAPHITI_400) == differ.SIG_API_ERROR_SCHEMA_INVALID


def test_the_other_wordings_of_the_same_break_are_caught() -> None:
    for message in (
        "Invalid schema for function 'extract_edges': 'required' is required to be supplied.",
        "Invalid schema for response_format 'X': 'additionalProperties' is required.",
    ):
        assert (
            differ._api_error_signature({"status_code": 400, "message": message})
            == differ.SIG_API_ERROR_SCHEMA_INVALID
        )


def test_the_sampling_and_tool_choice_400s_do_not_trigger_it() -> None:
    """Every 400 in the taxonomy is claimed by exactly one signature; a broad schema matcher
    that stole one of the others would send the loop after the wrong repair."""
    others = {
        differ.FORCED_TOOL_CHOICE_400: differ.SIG_API_ERROR_FORCED_TOOL_CHOICE,
        "Unsupported parameter: 'temperature' is not supported with this model.": (
            differ.SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS
        ),
        "Unsupported parameter: 'max_tokens' is not supported with this model. Use "
        "'max_completion_tokens' instead.": differ.SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP,
        "Function tools with reasoning_effort are not supported.": (
            differ.SIG_API_ERROR_TOOLS_REASONING
        ),
    }
    for message, expected in others.items():
        assert (
            differ._api_error_signature({"status_code": 400, "message": message}) == expected
        ), message


def test_the_signature_is_in_the_taxonomy_and_has_a_repair() -> None:
    assert differ.SIG_API_ERROR_SCHEMA_INVALID in differ.SIGNATURE_PRIORITY
    assert differ.SIG_API_ERROR_SCHEMA_INVALID in differ.SIGNATURE_DESCRIPTIONS
    assert differ.SIG_API_ERROR_SCHEMA_INVALID not in differ.SIGNATURES_WITHOUT_REPAIRS


# ---------------------------------------------------------------------------
# The three rules
# ---------------------------------------------------------------------------


def test_make_strict_satisfies_all_three_rules() -> None:
    strict = make_strict(LOOSE_SCHEMA)

    assert strict["additionalProperties"] is False                       # rule 1
    assert set(strict["required"]) == set(strict["properties"])          # rule 2
    assert strict["properties"]["valid_at"] == {                          # rule 3
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    # A property that WAS required keeps its own type: it was never omittable.
    assert strict["properties"]["edges"] == LOOSE_SCHEMA["properties"]["edges"]
    assert is_strict(strict)


def test_strict_violations_names_the_rules_the_api_names() -> None:
    assert strict_violations(LOOSE_SCHEMA) == ["additionalProperties", "required"]
    assert strict_violations(make_strict(LOOSE_SCHEMA)) == []


def test_nested_objects_are_made_strict_too() -> None:
    nested = {
        "type": "object",
        "properties": {"inner": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "required": ["inner"],
    }
    strict = make_strict(nested)
    inner = strict["properties"]["inner"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["a"]


def test_an_already_nullable_property_is_not_wrapped_twice() -> None:
    schema = {
        "type": "object",
        "properties": {"maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": [],
    }
    strict = make_strict(schema)
    assert strict["properties"]["maybe"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


# ---------------------------------------------------------------------------
# The candidate
# ---------------------------------------------------------------------------


def test_a_candidate_is_generated_for_a_loose_response_format(tmp_path: Path) -> None:
    agent = _agent(tmp_path / "a", params={"response_format": _response_format(LOOSE_SCHEMA)})
    candidates = generate_candidates(agent, [differ.SIG_API_ERROR_SCHEMA_INVALID])

    assert [p.id for p in candidates] == ["schema-strict-compat:ExtractedEdges"]
    edit = candidates[0].edits[0]
    assert edit.file == "agent.json"
    schema = json.loads(edit.new_content)["params"]["response_format"]["json_schema"]["schema"]
    assert is_strict(schema)
    assert schema["properties"]["valid_at"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }


def test_an_already_strict_schema_produces_no_candidate(tmp_path: Path) -> None:
    """A patch that changes nothing costs a screen run and tells its reader the schema was
    the problem when it was not."""
    agent = _agent(
        tmp_path / "a",
        params={"response_format": _response_format(make_strict(LOOSE_SCHEMA))},
    )
    assert generate_candidates(agent, [differ.SIG_API_ERROR_SCHEMA_INVALID]) == []


def test_a_tool_parameter_schema_is_repaired_too(tmp_path: Path) -> None:
    """p2-001: the schema that 400s is often a TOOL's, not a response format's."""
    agent = _agent(tmp_path / "a", tools=[_tool("extract_edges", LOOSE_SCHEMA)])
    candidates = generate_candidates(agent, [differ.SIG_API_ERROR_SCHEMA_INVALID])

    assert [p.id for p in candidates] == ["schema-strict-compat:extract_edges"]
    edit = candidates[0].edits[0]
    assert edit.file == "tools.json"
    written = json.loads(edit.new_content)
    assert is_strict(written[0]["function"]["parameters"])
    assert candidates[0].repair_type == "tool_schema_edit"


def test_only_the_offending_tool_is_edited(tmp_path: Path) -> None:
    """One candidate per named schema: the loop screens them as siblings and the patch a
    maintainer reads names one thing."""
    already = make_strict(LOOSE_SCHEMA)
    agent = _agent(
        tmp_path / "a",
        tools=[_tool("fine", already), _tool("broken", LOOSE_SCHEMA)],
    )
    candidates = generate_candidates(agent, [differ.SIG_API_ERROR_SCHEMA_INVALID])

    assert [p.id for p in candidates] == ["schema-strict-compat:broken"]
    written = json.loads(candidates[0].edits[0].new_content)
    assert written[0]["function"]["parameters"] == already, "the strict tool is untouched"


def test_an_agent_with_no_schemas_at_all_produces_no_candidate(tmp_path: Path) -> None:
    assert generate_candidates(
        _agent(tmp_path / "a"), [differ.SIG_API_ERROR_SCHEMA_INVALID]
    ) == []


# ---------------------------------------------------------------------------
# Rank and disclosure
# ---------------------------------------------------------------------------


def test_it_ranks_after_transport_and_before_capability_disabling() -> None:
    rank = rank_for("schema-strict-compat:ExtractedEdges")
    assert RANK_TRANSPORT < rank < RANK_CAPABILITY
    assert rank == RANK_BEHAVIOURAL


def test_it_carries_the_disclosure_that_downstream_must_accept_null() -> None:
    disclosures = disclosures_for("schema-strict-compat:ExtractedEdges")
    assert disclosures
    assert "changes_capability" in disclosures[0]
    assert "null" in disclosures[0]


def test_the_disclosure_survives_the_per_schema_suffix() -> None:
    """The id names the schema, so both lookups have to read the id's family, not the whole
    string — otherwise every schema repair would silently lose its disclosure."""
    assert disclosures_for("schema-strict-compat:anything") == disclosures_for(
        "schema-strict-compat:ExtractedEdges"
    )
    assert rank_for("schema-strict-compat:anything") == RANK_BEHAVIOURAL
