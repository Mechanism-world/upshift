"""The translation matrix is a test (DESIGN.md §G).

Every row of `agent_loop.TRANSLATION_TABLE` gets three kinds of test, and a coverage test at
the bottom fails if a row is added without them:

* POSITIVE — the canonical param reaches the target endpoint in that endpoint's own spelling
  and placement.
* NEGATIVE — a value the endpoint cannot carry is rejected or dropped, and the run RECORD says
  so. Nothing is silently dropped and nothing is silently weakened.
* PRECEDENCE — when the agent also wrote the native spelling (or its own `extra_body`), the
  more specific statement wins and the translated one stands down, with a note.

Provider facts asserted here are cited to docs/provider-matrix.md (checked 2026-09-07); the
400 wordings are the ones real runs recorded (rescue-ops `ops/cases`).
"""

from __future__ import annotations

import json

import pytest

from upshift import agent_loop, differ
from upshift.agent_loop import (
    ANTHROPIC_EFFORTS,
    CHAT,
    MESSAGES,
    OPENAI_EFFORTS,
    RESPONSES,
    TRANSLATION_TABLE,
    TranslationError,
    build_request,
    map_params,
    translate_params,
)
from upshift.providers.anthropic_provider import SAMPLING_PARAMS
from upshift.providers.base import ERROR_SDK_VALIDATION
from upshift.repair.playbook import (
    PATCH_DISCLOSURES,
    RANK_CAPABILITY,
    RANK_TRANSPORT,
    disclosures_for,
    generate_candidates,
    rank_for,
)

ENDPOINTS = (CHAT, RESPONSES, MESSAGES)

#: Rows whose POSITIVE/NEGATIVE/PRECEDENCE tests live in this file, and the test-name prefix
#: the coverage test looks for. A new row in TRANSLATION_TABLE must be added here WITH its
#: three tests, or `test_every_row_has_all_three_kinds_of_test` fails.
ROW_TEST_PREFIXES = {
    "reasoning": "test_reasoning",
    "output_cap": "test_output_cap",
    "tool_choice": "test_tool_choice",
    "sampling": "test_sampling",
    "seed": "test_seed",
    "state_linking": "test_state_linking",
}



def _config(endpoint: str, model: str, params: dict):
    from upshift.schemas import AgentConfig

    return AgentConfig(
        name="a", endpoint=endpoint, model=model, params=params, system_prompt="s",
        tools=[], max_turns=4, agent_dir="/nonexistent/agent",
    )


def _case():
    from upshift.schemas import Case

    return Case(
        id="c", description="", initial_state={}, user_messages=["hi"], checks=[], sim={}
    )


def _agent_dir() -> str:
    """The packaged booking agent: a real, checked-in agent dir the playbook can read."""
    from pathlib import Path

    return str(Path(__file__).resolve().parents[1] / "victim" / "booking_agent")


@pytest.fixture
def sdk_dropped_sampling_params(monkeypatch):
    """Pretend the installed anthropic SDK dropped temperature/top_p/top_k from
    `Messages.create()` (>= 1.1.0), whatever is actually installed."""
    monkeypatch.setattr(
        agent_loop, "messages_create_accepts", lambda name: name not in SAMPLING_PARAMS
    )


def dropped(result, name: str) -> dict | None:
    return next((d for d in result.dropped_params if d["name"] == name), None)


def note_for(result, param: str) -> str | None:
    return next((n["note"] for n in result.notes if n["param"] == param), None)


# ---------------------------------------------------------------------------
# Row: reasoning  (reasoning_effort <-> reasoning.effort <-> output_config.effort)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (CHAT, {"reasoning_effort": "high"}),
        (RESPONSES, {"reasoning": {"effort": "high"}}),
        (MESSAGES, {"output_config": {"effort": "high"}}),
    ],
)
def test_reasoning_positive_each_endpoint_gets_its_own_spelling(endpoint, expected):
    """chat spells it flat, `/v1/responses` nests it under `reasoning`, Anthropic under
    `output_config` (docs/provider-matrix.md)."""
    assert map_params(endpoint, {"reasoning_effort": "high"}) == expected


@pytest.mark.parametrize("value", OPENAI_EFFORTS)
def test_reasoning_positive_every_openai_rung_is_accepted(value):
    assert map_params(CHAT, {"reasoning_effort": value}) == {"reasoning_effort": value}


@pytest.mark.parametrize("value", ANTHROPIC_EFFORTS)
def test_reasoning_positive_every_anthropic_rung_is_accepted(value):
    assert map_params(MESSAGES, {"reasoning_effort": value}) == {"output_config": {"effort": value}}


@pytest.mark.parametrize("value", ["none", "minimal"])
def test_reasoning_negative_an_off_rung_is_refused_on_anthropic_not_substituted(value):
    """Anthropic has no `none`/`minimal` rung at all (docs/provider-matrix.md). Substituting
    the nearest one would run a DIFFERENT agent and report it as this one; substituting
    downward would report a model with reasoning disabled. Neither is allowed: it raises.
    """
    with pytest.raises(TranslationError) as excinfo:
        map_params(MESSAGES, {"reasoning_effort": value})

    error = excinfo.value
    assert error.param == "reasoning_effort"
    assert error.value == value
    assert error.endpoint == MESSAGES
    assert "will not substitute" in str(error)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_reasoning_negative_a_nonsense_value_never_silently_becomes_none(endpoint):
    with pytest.raises(TranslationError):
        map_params(endpoint, {"reasoning_effort": "turbo"})
    # and in particular the value it refused is still the one the agent declared: nothing
    # was quietly rewritten to a rung the endpoint does accept
    with pytest.raises(TranslationError) as excinfo:
        map_params(endpoint, {"reasoning_effort": "turbo"})
    assert excinfo.value.value == "turbo"
    assert "will not substitute" in str(excinfo.value)


def test_reasoning_negative_the_episode_records_the_refusal_as_a_harness_error():
    """A translation the loop refuses is a HARNESS failure: no request was built, nothing was
    sent, and the same config fails identically on both models of the pair. The record must
    say that, and the differ must not score it as a model regression."""

    class NeverCalled:
        def call(self, *args, **kwargs):  # pragma: no cover - the point is it is not called
            raise AssertionError("the provider must not be reached")

    class Backend:
        def execute(self, name, arguments):  # pragma: no cover
            return {}

        def state(self):
            return {}

    config = _config(MESSAGES, "claude-fable-5-1", {"reasoning_effort": "none"})
    result = agent_loop.run_episode(
        config, _case(), NeverCalled(), Backend(), rep=0, seed=0
    )

    assert result.api_error["type"] == "translation_error"
    assert result.api_error["status_code"] is None  # never a manufactured HTTP status
    assert result.api_error["param"] == "reasoning_effort"
    assert differ._api_error_signature(result.api_error) == differ.SIG_HARNESS_ERROR


@pytest.mark.parametrize(
    ("endpoint", "explicit"),
    [
        (RESPONSES, {"reasoning": {"effort": "max"}}),
        (MESSAGES, {"output_config": {"effort": "max"}}),
    ],
)
def test_reasoning_precedence_an_explicit_native_object_wins(endpoint, explicit):
    result = translate_params(endpoint, {"reasoning_effort": "low", **explicit})

    assert result.request_fields == explicit
    assert "wins" in note_for(result, "reasoning_effort")


def test_reasoning_precedence_a_native_object_without_an_effort_still_takes_the_translation():
    """Precedence is per FIELD, not per object: an agent that sets `reasoning.summary` and a
    canonical effort gets both, not one."""
    assert map_params(RESPONSES, {"reasoning_effort": "low", "reasoning": {"summary": "auto"}}) == {
        "reasoning": {"summary": "auto", "effort": "low"}
    }


# ---------------------------------------------------------------------------
# Row: output_cap  (max_tokens / max_completion_tokens / max_output_tokens)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", ["max_tokens", "max_completion_tokens"])
def test_output_cap_positive_responses_gets_max_output_tokens(declared):
    """`/v1/responses` spells it `max_output_tokens` and the OpenAI SDK raises TypeError for
    the chat spellings before a request is sent (docs/provider-matrix.md)."""
    assert map_params(RESPONSES, {declared: 4096}) == {"max_output_tokens": 4096}


@pytest.mark.parametrize("declared", ["max_completion_tokens", "max_output_tokens"])
def test_output_cap_positive_messages_gets_max_tokens(declared):
    assert map_params(MESSAGES, {declared: 4096}) == {"max_tokens": 4096}


@pytest.mark.parametrize("declared", ["max_tokens", "max_completion_tokens"])
def test_output_cap_positive_chat_keeps_the_spelling_it_was_written_with(declared):
    """Both are live on chat/completions — `max_tokens` deprecated in favour of
    `max_completion_tokens`, not removed — so there is nothing to translate and the API
    answers for the model."""
    assert map_params(CHAT, {declared: 512}) == {declared: 512}


def test_output_cap_negative_translation_never_raises_the_cap():
    """A translated cap carries its value verbatim. Nowhere does the table take a maximum, so
    routing an agent can only ever send the model the same cap or a smaller explicit one —
    never a larger one it was never authorised to spend."""
    for declared, endpoint, target in [
        ({"max_tokens": 100}, RESPONSES, "max_output_tokens"),
        ({"max_completion_tokens": 100}, MESSAGES, "max_tokens"),
    ]:
        assert map_params(endpoint, declared)[target] == 100
    # a smaller explicit native cap wins even though a bigger foreign one is declared
    assert map_params(RESPONSES, {"max_completion_tokens": 9999, "max_output_tokens": 8}) == {
        "max_output_tokens": 8
    }


def test_output_cap_negative_a_superseded_spelling_is_reported_not_silently_dropped():
    result = translate_params(RESPONSES, {"max_tokens": 1, "max_completion_tokens": 2})

    assert result.request_fields == {"max_output_tokens": 2}
    assert "superseded by 'max_completion_tokens'" in note_for(result, "max_tokens")


@pytest.mark.parametrize("order", [True, False])
def test_output_cap_precedence_the_native_spelling_wins_in_either_order(order):
    declared = (
        {"max_output_tokens": 99, "max_completion_tokens": 1}
        if order
        else {"max_completion_tokens": 1, "max_output_tokens": 99}
    )
    assert map_params(RESPONSES, declared) == {"max_output_tokens": 99}


def test_output_cap_precedence_survives_into_the_built_request():
    """The whole point: a routed agent's request is one /v1/responses accepts."""
    request = build_request(
        RESPONSES, "gpt-5.6-luna", {"max_completion_tokens": 4096}, [], [{"role": "user", "content": "hi"}]
    )
    assert request["max_output_tokens"] == 4096
    assert "max_completion_tokens" not in request
    assert "max_tokens" not in request


# ---------------------------------------------------------------------------
# Row: tool_choice  (chat nested <-> responses flat <-> Anthropic shapes)
# ---------------------------------------------------------------------------


NESTED_FORCED = {"type": "function", "function": {"name": "book_flight"}}


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (CHAT, NESTED_FORCED),
        (RESPONSES, {"type": "function", "name": "book_flight"}),
        (MESSAGES, {"type": "tool", "name": "book_flight"}),
    ],
)
def test_tool_choice_positive_a_forced_named_tool_reaches_every_endpoint(endpoint, expected):
    assert map_params(endpoint, {"tool_choice": NESTED_FORCED}) == {"tool_choice": expected}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("required", {"type": "any"}), ("auto", {"type": "auto"}), ("none", {"type": "none"})],
)
def test_tool_choice_positive_the_openai_strings_become_anthropic_objects(value, expected):
    """`none` IS supported on Messages (docs/provider-matrix.md), so it translates rather than
    being dropped."""
    assert map_params(MESSAGES, {"tool_choice": value}) == {"tool_choice": expected}


def test_tool_choice_negative_translation_never_removes_a_forced_choice():
    """A forced tool choice is a CAPABILITY, not a spelling: under it the API guarantees the
    turn is a tool call. Translation re-spells it for every endpoint and never drops it, even
    for Fable 5.1, which answers 400 `tool_choice: type "tool" and "any" are not supported for
    this model.` — removing it is a REPAIR with a disclosed changed guarantee, and the
    disclosure only exists because translation left the decision to the repair loop.
    """
    for endpoint in ENDPOINTS:
        for value in ("required", NESTED_FORCED, {"type": "any"}):
            result = translate_params(endpoint, {"tool_choice": value})
            assert "tool_choice" in result.request_fields
            assert result.dropped_params == []


def test_tool_choice_negative_an_unrecognised_shape_is_forwarded_for_the_api_to_reject():
    """Rewriting a value upshift does not understand would replace the API's own 400 — the
    evidence — with a guess."""
    weird = {"type": "telepathy", "vibe": "strong"}
    for endpoint in ENDPOINTS:
        assert map_params(endpoint, {"tool_choice": weird}) == {"tool_choice": weird}


@pytest.mark.parametrize(
    ("endpoint", "native"),
    [
        (RESPONSES, {"type": "function", "name": "book_flight"}),
        (MESSAGES, {"type": "tool", "name": "book_flight"}),
        (MESSAGES, {"type": "any", "disable_parallel_tool_use": True}),
    ],
)
def test_tool_choice_precedence_a_value_already_native_passes_through_untouched(endpoint, native):
    assert map_params(endpoint, {"tool_choice": native}) == {"tool_choice": native}


# ---------------------------------------------------------------------------
# Row: sampling  (temperature / top_p / top_k)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", [CHAT, RESPONSES])
def test_sampling_positive_openai_endpoints_take_them_top_level(endpoint):
    assert map_params(endpoint, {"temperature": 0.4, "top_p": 0.9}) == {
        "temperature": 0.4,
        "top_p": 0.9,
    }


def test_sampling_positive_messages_routes_them_through_extra_body(sdk_dropped_sampling_params):
    """`anthropic` >= 1.1.0 dropped them from `Messages.create()`; `extra_body` is the SDK's
    documented escape hatch and the wire field is unchanged, so the API — not the SDK —
    decides (docs/provider-matrix.md)."""
    assert map_params(MESSAGES, {"temperature": 0.4, "top_p": 0.9, "top_k": 20}) == {
        "extra_body": {"temperature": 0.4, "top_p": 0.9, "top_k": 20}
    }


def test_sampling_negative_a_rejecting_model_is_never_pre_empted_by_dropping_the_param():
    """Real 400s: `Unsupported parameter: 'temperature' is not supported with this model.` and
    `Unsupported value: 'temperature' does not support 0.0 with this model.` Dropping the param
    here would hide exactly the break upshift exists to find, so it is sent as declared and
    the record says so."""
    result = translate_params(RESPONSES, {"temperature": 0.0})

    assert result.request_fields == {"temperature": 0.0}
    assert result.dropped_params == []
    assert "sent as declared, not dropped" in note_for(result, "temperature")


def test_sampling_negative_one_hidden_in_extra_body_is_still_reported():
    """`extra_body` goes to the wire as raw JSON, so a sampling param inside it is every bit as
    real as a top-level one — and invisible unless it is named."""
    result = translate_params(RESPONSES, {"extra_body": {"top_k": 5}})

    assert result.request_fields == {"extra_body": {"top_k": 5}}  # untouched
    assert "sampling param sent in the agent's own extra_body" in note_for(
        result, "extra_body.top_k"
    )


def test_sampling_precedence_a_hand_written_extra_body_wins(sdk_dropped_sampling_params):
    result = translate_params(
        MESSAGES,
        {"temperature": 0.4, "top_k": 20, "extra_body": {"temperature": 0.9, "beta": True}},
    )

    assert result.request_fields == {"extra_body": {"temperature": 0.9, "beta": True, "top_k": 20}}
    assert "the agent's own extra_body value wins" in note_for(result, "temperature")


def test_sampling_precedence_the_value_reaches_the_wire_exactly_once(sdk_dropped_sampling_params):
    """A value in both the top level and `extra_body` would be sent twice and could disagree."""
    request = build_request(MESSAGES, "claude-fable-5-1", {"temperature": 0.2}, [], [])

    assert "temperature" not in request
    assert request["extra_body"] == {"temperature": 0.2}


# ---------------------------------------------------------------------------
# Row: seed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", [CHAT, RESPONSES])
def test_seed_positive_passes_through_where_the_endpoint_has_the_parameter(endpoint):
    assert map_params(endpoint, {"seed": 42}) == {"seed": 42}


def test_seed_positive_records_best_effort_determinism_never_deterministic():
    """OpenAI's own guarantee: "our system will make a best effort to sample
    deterministically ... Determinism is not guaranteed." upshift records that wording's
    meaning and never claims a seeded run is reproducible."""
    result = translate_params(CHAT, {"seed": 42})

    assert result.determinism == "best_effort"
    assert result.record()["determinism"] == "best_effort"
    assert "deterministic" not in json.dumps(result.record())


def test_seed_negative_is_dropped_with_a_reason_on_messages():
    """The Anthropic Messages API has no `seed`; forwarding it would be an SDK TypeError, and
    dropping it silently would let a reader believe the run was seeded."""
    result = translate_params(MESSAGES, {"seed": 42})

    assert "seed" not in result.request_fields
    assert dropped(result, "seed")["reason"] == "the Anthropic Messages API has no `seed` parameter"
    assert result.determinism is None


def test_seed_precedence_nothing_to_arbitrate_the_canonical_name_is_the_native_one():
    assert map_params(CHAT, {"seed": 42})["seed"] == 42
    assert build_request(CHAT, "gpt-5.6-sol", {"seed": 42}, [], [])["seed"] == 42


# ---------------------------------------------------------------------------
# Row: state_linking  (previous_response_id / conversation / store / metadata / user)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "param", ["previous_response_id", "conversation", "store", "metadata", "user"]
)
def test_state_linking_positive_is_dropped_on_every_endpoint_with_a_recorded_reason(
    endpoint, param
):
    """rescue-ops `prisma-41-review` strips `previous_response_id`/`conversation` and pins
    `store` to false for the same reason upshift does: a review — like a rep — is ONE call
    with one rendered prompt, and a valid id would bring in turns it never rendered."""
    result = translate_params(endpoint, {param: "x"})

    assert param not in result.request_fields
    entry = dropped(result, param)
    assert entry is not None
    assert "one-shot" in entry["reason"]
    assert result.record()["dropped_params"] == [entry]


def test_state_linking_negative_a_dropped_store_does_not_unpin_the_responses_default():
    """`build_request` pins `store: false` on /v1/responses; dropping the agent's own value
    must leave that pin standing, not remove retention control altogether."""
    request = build_request(RESPONSES, "gpt-5.6-sol", {"store": True}, [], [])

    assert request["store"] is False


def test_state_linking_negative_upshifts_own_params_are_not_swept_up():
    """`prompt_cache_key` is a routing hint upshift injects, not the agent's state linking; it
    survives translation and is not reported as the agent's passthrough."""
    result = translate_params(CHAT, {"prompt_cache_key": "upshift-abc"})

    assert result.request_fields == {"prompt_cache_key": "upshift-abc"}
    assert result.dropped_params == []
    assert result.passthrough_params == []


def test_state_linking_precedence_one_in_extra_body_is_reported_but_never_rewritten():
    """`extra_body` is the agent's own more-specific statement about the wire, so it is
    reported, not edited — otherwise a reader could not tell what was actually sent."""
    result = translate_params(RESPONSES, {"extra_body": {"previous_response_id": "resp_1"}})

    assert result.request_fields == {"extra_body": {"previous_response_id": "resp_1"}}
    assert "sent verbatim in the agent's own extra_body" in note_for(
        result, "extra_body.previous_response_id"
    )


# ---------------------------------------------------------------------------
# Unknown params, and the record itself
# ---------------------------------------------------------------------------


def test_an_unknown_param_passes_through_and_is_listed():
    result = translate_params(CHAT, {"parallel_tool_calls": False, "service_tier": "flex"})

    assert result.request_fields == {"parallel_tool_calls": False, "service_tier": "flex"}
    assert sorted(result.passthrough_params) == ["parallel_tool_calls", "service_tier"]


def test_the_record_is_empty_when_there_was_nothing_to_say():
    """The overwhelmingly common case must not grow every rep record on disk."""
    assert translate_params(CHAT, {"reasoning_effort": "medium"}).record() == {}
    assert translate_params(MESSAGES, {}).record() == {}


def test_the_episode_records_one_translation_note_per_api_call():
    """Index-aligned with `api_calls`, so a reader can tell WHICH request dropped what."""

    class Provider:
        def call(self, endpoint, request, seed_key, sim_context=None):
            return {"choices": [{"message": {"content": "done"}}]}

    class Backend:
        def execute(self, name, arguments):  # pragma: no cover
            return {}

        def state(self):
            return {}

    config = _config(CHAT, "gpt-5.6-sol", {"store": True, "seed": 7})
    result = agent_loop.run_episode(config, _case(), Provider(), Backend(), rep=0, seed=0)

    assert len(result.translations) == len(result.api_calls) == 1
    assert result.translations[0]["dropped_params"][0]["name"] == "store"
    assert result.translations[0]["determinism"] == "best_effort"


def test_every_row_has_all_three_kinds_of_test():
    """A row added to TRANSLATION_TABLE without positive/negative/precedence tests fails here,
    which is the point of making the matrix a test."""
    names = [name for name in globals() if name.startswith("test_")]
    for row in TRANSLATION_TABLE:
        prefix = ROW_TEST_PREFIXES.get(row.name)
        assert prefix, f"row {row.name!r} has no tests in {__file__}"
        mine = [n for n in names if n.startswith(prefix)]
        for kind in ("positive", "negative", "precedence"):
            assert any(f"_{kind}_" in n for n in mine), f"row {row.name!r} has no {kind} test"


def test_every_row_covers_every_endpoint():
    """A row that forgets an endpoint would fall through to passthrough and be sent verbatim
    to an API that does not have the field."""
    for row in TRANSLATION_TABLE:
        assert set(row.fields) == set(ENDPOINTS), row.name
        assert set(row.placement) == set(ENDPOINTS), row.name
        assert row.precedence, f"row {row.name!r} does not state its precedence rule"
        if any(target is None for target in row.fields.values()):
            assert row.reason, f"row {row.name!r} drops a param without a recorded reason"


# ---------------------------------------------------------------------------
# Provider error classification: a local SDK refusal is not a provider verdict
# ---------------------------------------------------------------------------


def test_a_local_sdk_refusal_is_never_given_a_manufactured_http_status():
    """`Responses.create() got an unexpected keyword argument 'max_completion_tokens'` is
    raised before any bytes leave the machine. Recording it as a 400 would tell a reader the
    provider rejected the agent — and would let the differ score a harness fault against the
    candidate model."""
    import openai

    class Boom:
        def create(self, **kwargs):
            raise TypeError(
                "Responses.create() got an unexpected keyword argument 'max_completion_tokens'"
            )

    from upshift.providers.base import ProviderAPIError
    from upshift.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider()
    provider._client = type("C", (), {"responses": Boom(), "chat": None})()

    with pytest.raises(ProviderAPIError) as excinfo:
        provider.call("responses", {"model": "gpt-5.6-sol"}, "k")

    error = excinfo.value
    assert error.status_code is None
    assert error.error_type == ERROR_SDK_VALIDATION
    assert "before it reached the wire" in error.message
    assert openai  # the SDK is importable; the failure above is local, not transport


def test_the_differ_calls_an_sdk_validation_error_a_harness_error_not_a_regression():
    signature = differ._api_error_signature(
        {"status_code": None, "message": "unexpected keyword argument", "type": ERROR_SDK_VALIDATION}
    )

    assert signature == differ.SIG_HARNESS_ERROR
    assert "never reached the provider" in differ.SIGNATURE_DESCRIPTIONS[differ.SIG_HARNESS_ERROR]
    # ranked first: if the request never went out, nothing else in the run is model evidence
    assert differ.SIGNATURE_PRIORITY[0] == differ.SIG_HARNESS_ERROR
    # and no repair is generated for it
    assert generate_candidates(_agent_dir(), [differ.SIG_HARNESS_ERROR]) == []


def test_an_http_error_keeps_its_status_and_the_apis_own_wording():
    """The counterpart: when the provider DID answer, the status and message are evidence and
    are recorded verbatim."""
    message = (
        "Function tools with reasoning_effort are not supported for gpt-5.6-sol in "
        "/v1/chat/completions. To use function tools, use /v1/responses or set "
        "reasoning_effort to 'none'."
    )
    err = {"status_code": 400, "message": message, "type": "api_status_error"}

    assert differ._api_error_signature(err) == differ.SIG_API_ERROR_TOOLS_REASONING


def test_a_rejected_effort_value_is_classified_on_the_apis_own_wording():
    """Real 400 (rescue-ops): the model lists the ladder it does have. Before v0.5 this fell
    into the generic bucket and told a reader nothing."""
    message = (
        "Unsupported value: 'minimal' is not supported with the 'gpt-5.6-luna' model. "
        "Supported values are: 'none', 'low', 'medium', 'high', 'xhigh', and 'max'."
    )

    assert differ._api_error_signature(
        {"status_code": 400, "message": message, "type": "api_status_error"}
    ) == differ.SIG_API_ERROR_UNSUPPORTED_EFFORT_VALUE
    # ... and is not confused with the sampling 400, whose wording is different
    sampling = (
        "Unsupported value: 'temperature' does not support 0.0 with this model. Only the "
        "default (1) value is supported."
    )
    assert differ._api_error_signature(
        {"status_code": 400, "message": sampling, "type": "api_status_error"}
    ) == differ.SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS


# ---------------------------------------------------------------------------
# Repair disclosures and ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch_id",
    ["remove-forced-tool-choice", "drop-sampling-params", "drop-token-cap-param",
     "reasoning-effort-none"],
)
def test_every_capability_changing_repair_discloses_what_it_changes(patch_id):
    disclosures = disclosures_for(patch_id)

    assert disclosures, f"{patch_id} changes a guarantee and must say so"
    assert any(d.startswith("changes_capability:") for d in disclosures)
    assert rank_for(patch_id) == RANK_CAPABILITY


@pytest.mark.parametrize("patch_id", ["raise-effort-one-rung", "reasoning-effort-high"])
def test_every_effort_raising_repair_discloses_its_cost(patch_id):
    assert any(d.startswith("changes_cost:") for d in disclosures_for(patch_id))


def test_disabling_reasoning_is_never_presented_as_equivalent_to_endpoint_routing():
    """The 400 offers both fixes in one sentence ("use /v1/responses OR set reasoning_effort
    to 'none'"), which reads as if they were interchangeable. They are not: one moves the same
    agent to an endpoint that supports it, the other turns its reasoning off."""
    disclosure = " ".join(disclosures_for("reasoning-effort-none"))

    assert "disables reasoning" in disclosure
    assert "NOT equivalent" in disclosure


def test_a_pure_spelling_or_routing_fix_has_nothing_to_disclose():
    for patch_id in ("route-to-responses", "rename-token-cap-param"):
        assert disclosures_for(patch_id) == []
        assert rank_for(patch_id) == RANK_TRANSPORT


def test_endpoint_routing_is_ordered_before_turning_reasoning_off():
    """The loop accepts the first candidate that restores the broken cases, so order decides
    which fix ships."""
    candidates = [c.id for c in generate_candidates(_agent_dir(), ["api_error_tools_reasoning"])]

    assert candidates.index("route-to-responses") < candidates.index("reasoning-effort-none")


def test_candidates_are_ordered_transport_first_capability_last():
    signatures = [
        "api_error_tools_reasoning",
        "api_error_unsupported_sampling_params",
        "wrong_or_missing_tool_call",
    ]
    ranks = [rank_for(c.id) for c in generate_candidates(_agent_dir(), signatures)]

    assert ranks == sorted(ranks)


def test_no_disclosure_is_registered_for_a_patch_id_the_playbook_cannot_emit():
    """A stale disclosure is worse than none: it would describe a repair nobody ships."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/upshift/repair/playbook.py").read_text()
    emitted = set(re.findall(r'\n\s+add\(\s*\n?\s*"([a-z0-9-]+)"', source))

    assert set(PATCH_DISCLOSURES) <= emitted
