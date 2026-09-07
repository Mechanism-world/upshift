"""Generic tool-calling executor for one episode (one rep of one case).

Owns message building, request shaping and tool-call parsing for all three supported endpoints
(`chat_completions`, `responses` and Anthropic's `messages`); the provider only performs the
transport. See DESIGN.md.

Wire formats handled here are the exact shapes the sim provider emits and the OpenAI SDK
returns, so a transcript recorded against the sim is structurally identical to a real one.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any

from upshift.capture import continuation
from upshift.providers.anthropic_provider import SAMPLING_PARAMS, messages_create_accepts
from upshift.providers.base import ProviderAPIError
from upshift.schemas import AgentConfig, APICall, Case, ToolExecution

CHAT = "chat_completions"
RESPONSES = "responses"
MESSAGES = "messages"

#: Anthropic requires max_tokens; thinking counts against it, so the default is generous.
DEFAULT_MAX_TOKENS = 8192

#: Anthropic caches only prefixes carrying an explicit `cache_control` mark. Prefixes shorter
#: than the model's minimum cacheable length (512 tokens) are simply not cached — no error.
EPHEMERAL = {"type": "ephemeral"}

INVALID_ARGS_ERROR = {"error": "invalid JSON in tool call arguments"}

#: Recorded as the result of a terminal tool call (AgentConfig.terminal_tools). The framework
#: never produced one, so upshift does not invent one and does not run the replay backend: the
#: episode ends on the call, exactly as the captured conversation did.
TERMINAL_TOOL_RESULT = {"terminal_tool": "the framework ended the conversation on this call"}

#: chat/completions spellings of the output-token cap. On `/v1/responses` the field is
#: `max_output_tokens`; both of these are rejected there, so map_params translates them.
TOKEN_CAP_PARAMS = ("max_tokens", "max_completion_tokens")


@dataclass
class EpisodeResult:
    """Everything one episode produced; the runner combines this with check results to build
    a schemas.RepRecord."""

    api_calls: list[APICall] = field(default_factory=list)
    tool_executions: list[ToolExecution] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)
    final_message: str = ""
    api_error: dict[str, Any] | None = None
    resolved_model: str | None = None
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    )
    latency_s: float = 0.0
    #: stop_reason of the last successful response (Anthropic `messages` only; None elsewhere).
    stop_reason: str | None = None
    #: What the translation matrix did to the agent's params, per API call, index-aligned with
    #: `api_calls`: `{"dropped_params": [{name, reason}], "passthrough_params": [...],
    #: "notes": [{param, note}], "determinism": "best_effort"}` — see `Translation.record`.
    #: An empty dict where a call needed no translation, so the common case costs nothing.
    #: It rides alongside `api_calls` rather than inside `APICall` because `schemas.APICall`
    #: belongs to the recorder's contract; the recorder folds these into the rep record.
    translations: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def params_for_turn(
    params: dict[str, Any], turn_params: list[dict[str, Any]] | None, index: int
) -> dict[str, Any]:
    """`params` with assistant turn `index`'s overrides applied over it.

    The sequence is read by INDEX and its last entry repeats, so a two-entry list describes
    "turn 1 like this, every turn after it like that" — the force-then-`auto` shape every
    structured-output and routing agent has (`AgentConfig.turn_params`). A `None` override
    UNSETS the param for that turn, because "the framework did not send this field" is a
    different request from "the framework sent `auto`", and a capture records which.

    An empty/absent sequence returns `params` unchanged: the identity every hand-written
    agent takes.
    """
    if not turn_params:
        return params
    overrides = turn_params[index] if index < len(turn_params) else turn_params[-1]
    if not overrides:
        return params
    merged = dict(params or {})
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# ---------------------------------------------------------------------------
# The translation matrix (DESIGN.md §G)
#
# One declarative table says, for every canonical parameter upshift understands, how it is
# spelled and where it is placed on each of the three endpoints, which values are legal
# there, and what happens when the agent ALSO wrote the native spelling by hand. Every row
# has a positive, a negative and a precedence test in tests/test_translation_matrix.py.
#
# Two invariants the table exists to enforce:
#   * Nothing is silently dropped. Every drop lands in `Translation.dropped_params` with a
#     reason, every unrecognised key in `passthrough_params`, and both reach the run record.
#   * Nothing is silently weakened. An effort value the target endpoint does not accept is a
#     TranslationError, never a substitution — least of all a substitution to "none", which
#     would turn reasoning OFF and report the result as if the same agent had been measured.
# ---------------------------------------------------------------------------


class TranslationError(ValueError):
    """A canonical parameter cannot be expressed on the target endpoint without changing what
    the agent does. Raised while the request is being built, so it is recorded against the
    episode (as a `translation_error`, a harness failure) and never mistaken for a model
    regression. A ValueError because every authoring/config error in upshift is one.
    """

    def __init__(self, param: str, value: Any, endpoint: str, reason: str) -> None:
        super().__init__(f"{param}={value!r} cannot be translated to {endpoint}: {reason}")
        self.param = param
        self.value = value
        self.endpoint = endpoint
        self.reason = reason


#: Where a translated value lands in the request body.
PLACE_TOP = "top_level"  # a top-level request field
PLACE_NESTED = "nested"  # a field inside a top-level object, e.g. reasoning.effort
PLACE_EXTRA_BODY = "extra_body"  # the SDK's raw-body escape hatch
PLACE_DROPPED = "dropped"  # not forwarded at all; recorded in dropped_params

#: Reasoning-effort ladders the ENDPOINT accepts, low to high. Per-MODEL gaps are deliberately
#: not encoded: gpt-5.6-luna answers `Unsupported value: 'minimal' is not supported with the
#: 'gpt-5.6-luna' model. Supported values are: 'none', 'low', 'medium', 'high', 'xhigh', and
#: 'max'.` (rescue-ops ops/cases evidence), and letting the API answer is more honest than a
#: model table upshift would have to keep current. What the table DOES enforce is the
#: cross-provider gap: Anthropic has no "none"/"minimal" rung at all, so routing an OpenAI
#: agent that disabled reasoning onto `messages` is a translation error rather than a silent
#: promotion to a thinking model — and vice versa, "none" is never invented for Anthropic.
OPENAI_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Server-side state and identity linking. upshift replays every case from scratch, N times,
#: against two models; a request that attaches to a stored response, a conversation object or
#: an end-user identity is not the one-shot request the contract under test describes, and
#: forwarding one would silently import turns upshift never rendered into the evidence
#: (rescue-ops `prisma-41-review`: `previous_response_id` and `conversation` are stripped
#: unconditionally and `store` pinned to false, for exactly this reason).
STATE_LINKING_PARAMS = ("previous_response_id", "conversation", "store", "metadata", "user")
STATE_LINKING_REASON = (
    "upshift replays each case one-shot, N times, from an empty conversation: a request that "
    "links to server-side state or an end-user identity is not the request under test"
)

#: Endpoints that accept `seed`. Anthropic's Messages API has no seed parameter.
SEED_ACCEPTED = {CHAT: True, RESPONSES: True, MESSAGES: False}
#: What upshift claims about a seeded run. Never "deterministic": no provider promises it.
DETERMINISM_BEST_EFFORT = "best_effort"

#: Ours, not the agent's: the OpenAI provider injects a cache-routing hint. It survives
#: translation untouched and is not reported as a passthrough (it is not the agent's config).
UPSHIFT_OWNED_PARAMS = ("prompt_cache_key",)


@dataclass(frozen=True)
class ParamRow:
    """One canonical parameter family and its per-endpoint treatment.

    `fields` maps endpoint -> the request field the value ends up in (dotted for a nested
    field, e.g. ``reasoning.effort``) or None when the endpoint drops it. `allowed` maps
    endpoint -> the legal values, when the row constrains them. `precedence` is the rule that
    decides who wins when the agent also wrote the native spelling by hand — prose here, and
    an executable test per row in tests/test_translation_matrix.py.
    """

    name: str
    params: tuple[str, ...]
    fields: dict[str, str | None]
    placement: dict[str, str]
    allowed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    precedence: str = "the canonical param is the only spelling; nothing to arbitrate"
    reason: str = ""


TRANSLATION_TABLE: tuple[ParamRow, ...] = (
    ParamRow(
        name="reasoning",
        params=("reasoning_effort",),
        fields={
            CHAT: "reasoning_effort",
            RESPONSES: "reasoning.effort",
            MESSAGES: "output_config.effort",
        },
        placement={CHAT: PLACE_TOP, RESPONSES: PLACE_NESTED, MESSAGES: PLACE_NESTED},
        allowed={CHAT: OPENAI_EFFORTS, RESPONSES: OPENAI_EFFORTS, MESSAGES: ANTHROPIC_EFFORTS},
        precedence=(
            "an explicit `reasoning` / `output_config` object in the agent's params is the "
            "more specific statement and keeps its own effort"
        ),
    ),
    ParamRow(
        name="output_cap",
        params=("max_tokens", "max_completion_tokens", "max_output_tokens"),
        fields={
            # chat/completions historically took `max_tokens` and now `max_completion_tokens`
            # ("`max_tokens` is now deprecated in favor of `max_completion_tokens`" — OpenAI
            # chat reference); both are live spellings there, so whichever the agent was
            # written with is kept and the API answers for the model. `"*"` = no translation.
            CHAT: "*",
            RESPONSES: "max_output_tokens",
            MESSAGES: "max_tokens",
        },
        placement={CHAT: PLACE_TOP, RESPONSES: PLACE_TOP, MESSAGES: PLACE_TOP},
        precedence=(
            "the endpoint's own spelling wins over a translated one; among foreign spellings "
            "`max_completion_tokens` (the newer one) wins. A translated cap carries its value "
            "verbatim: translation may LOWER what reaches the model (a smaller explicit cap "
            "wins) but never raises it"
        ),
    ),
    ParamRow(
        name="tool_choice",
        params=("tool_choice",),
        fields={CHAT: "tool_choice", RESPONSES: "tool_choice", MESSAGES: "tool_choice"},
        placement={CHAT: PLACE_TOP, RESPONSES: PLACE_TOP, MESSAGES: PLACE_TOP},
        precedence=(
            "a value already in the target endpoint's own shape passes through untouched; an "
            "unrecognised one is forwarded so the API's own 400 is what gets recorded"
        ),
        reason=(
            "a forced tool choice is a CAPABILITY of the agent, not a spelling: translation "
            "re-spells it for the target endpoint and never removes it. Removing it is a "
            "repair with a disclosed changed guarantee (repair/playbook.py)"
        ),
    ),
    ParamRow(
        name="sampling",
        params=SAMPLING_PARAMS,
        fields={CHAT: "*", RESPONSES: "*", MESSAGES: "*"},
        placement={CHAT: PLACE_TOP, RESPONSES: PLACE_TOP, MESSAGES: PLACE_EXTRA_BODY},
        precedence=(
            "an `extra_body` the agent wrote by hand is the more specific statement and keeps "
            "its own value; on the OpenAI endpoints the param is passed through even for a "
            "reasoning model that rejects it, so the API's own 400 is the evidence"
        ),
    ),
    ParamRow(
        name="seed",
        params=("seed",),
        fields={CHAT: "seed", RESPONSES: "seed", MESSAGES: None},
        placement={CHAT: PLACE_TOP, RESPONSES: PLACE_TOP, MESSAGES: PLACE_DROPPED},
        precedence="passed through verbatim where the endpoint has the parameter",
        reason="the Anthropic Messages API has no `seed` parameter",
    ),
    ParamRow(
        name="state_linking",
        params=STATE_LINKING_PARAMS,
        fields={CHAT: None, RESPONSES: None, MESSAGES: None},
        placement={CHAT: PLACE_DROPPED, RESPONSES: PLACE_DROPPED, MESSAGES: PLACE_DROPPED},
        precedence="dropped on every endpoint; there is nothing to arbitrate",
        reason=STATE_LINKING_REASON,
    ),
)

#: canonical param name -> its row. Built once; the rows are the authority.
_ROW_FOR_PARAM: dict[str, ParamRow] = {
    param: row for row in TRANSLATION_TABLE for param in row.params
}


@dataclass
class Translation:
    """What `translate_params` produced, and everything the record must say about it."""

    request_fields: dict[str, Any] = field(default_factory=dict)
    #: [{"name": str, "reason": str}] — every param NOT forwarded, and why.
    dropped_params: list[dict[str, str]] = field(default_factory=list)
    #: params the table does not know, forwarded verbatim under their own name.
    passthrough_params: list[str] = field(default_factory=list)
    #: [{"param": str, "note": str}] — anything a reader of the record needs to know that is
    #: not a drop: a value routed into extra_body, a sampling param an OpenAI reasoning model
    #: may reject, a hand-written extra_body that beat a canonical param.
    notes: list[dict[str, str]] = field(default_factory=list)
    #: "best_effort" when the request carries a seed; None when it does not. Never
    #: "deterministic" — no provider documents seeded sampling as reproducible.
    determinism: str | None = None

    def record(self) -> dict[str, Any]:
        """The translation note for the run record. Empty dict when there is nothing to say,
        so records for the overwhelmingly common no-translation case do not grow."""
        out: dict[str, Any] = {}
        if self.dropped_params:
            out["dropped_params"] = [dict(d) for d in self.dropped_params]
        if self.passthrough_params:
            out["passthrough_params"] = list(self.passthrough_params)
        if self.notes:
            out["notes"] = [dict(n) for n in self.notes]
        if self.determinism:
            out["determinism"] = self.determinism
        return out


def map_params(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Canonical params -> endpoint-specific request fields. Unknown keys pass through.

    The request half of `translate_params`; use that one when the record needs to say what
    was dropped or moved.
    """
    return translate_params(endpoint, params).request_fields


def translate_params(endpoint: str, params: dict[str, Any]) -> Translation:
    """Run the translation matrix over one param dict.

    Raises TranslationError when a value cannot be carried to `endpoint` without changing the
    agent's behaviour. Never returns a request that quietly differs from what the agent asked
    for: anything not forwarded is in `dropped_params`, anything unrecognised is in
    `passthrough_params`, anything moved is in `notes`.
    """
    result = Translation()
    out = result.request_fields
    deferred: dict[str, Any] = {}  # rows that must land AFTER every explicit value has

    for key, value in (params or {}).items():
        row = _ROW_FOR_PARAM.get(key)
        if row is None:
            if key == "extra_body":
                out["extra_body"] = copy.deepcopy(value)
            elif key in UPSHIFT_OWNED_PARAMS:
                out[key] = copy.deepcopy(value)
            else:
                out[key] = copy.deepcopy(value)
                result.passthrough_params.append(key)
            continue
        target = row.fields.get(endpoint, "")
        if target is None:
            result.dropped_params.append({"name": key, "reason": row.reason})
            continue
        if row.name in ("reasoning", "output_cap", "sampling"):
            # Order-sensitive: these arbitrate against a native spelling or an extra_body that
            # may appear later in the dict, so they land once the loop has finished.
            _defer(deferred, row, key, value, endpoint)
        elif row.name == "tool_choice":
            out["tool_choice"] = _TOOL_CHOICE_TRANSLATORS[endpoint](value)
        elif row.name == "seed":
            out[key] = copy.deepcopy(value)
            result.determinism = DETERMINISM_BEST_EFFORT
        else:  # pragma: no cover - every row above is handled; a new one must be too
            raise TranslationError(key, value, endpoint, f"row {row.name!r} has no handler")

    _land_reasoning(endpoint, deferred, result)
    _land_output_cap(endpoint, deferred, result)
    _land_sampling(endpoint, deferred, result)
    _inspect_extra_body(endpoint, out, result)
    return result


def _defer(
    deferred: dict[str, Any], row: ParamRow, key: str, value: Any, endpoint: str
) -> None:
    if row.name == "reasoning":
        _check_allowed(row, key, value, endpoint)
        deferred["reasoning"] = value
    elif row.name == "output_cap":
        # `max_output_tokens` on /v1/responses and `max_tokens` on messages are the endpoint's
        # own spelling: an explicit one wins over anything translated. Among foreign
        # spellings, the newer `max_completion_tokens` wins.
        caps = deferred.setdefault("output_cap", {})
        caps[key] = copy.deepcopy(value)
    else:
        deferred.setdefault("sampling", {})[key] = copy.deepcopy(value)


def _check_allowed(row: ParamRow, key: str, value: Any, endpoint: str) -> None:
    allowed = row.allowed.get(endpoint)
    if allowed and value not in allowed:
        raise TranslationError(
            key,
            value,
            endpoint,
            f"this endpoint accepts {', '.join(repr(v) for v in allowed)}. upshift will not "
            "substitute a different level: a run at another effort measures another agent, "
            "and substituting the lowest rung would report a model with reasoning disabled "
            "as if it were the one under test",
        )


def _land_reasoning(endpoint: str, deferred: dict[str, Any], result: Translation) -> None:
    """Canonical `reasoning_effort` -> the endpoint's nested effort field.

    chat/completions already spells it that way. On `/v1/responses` it is `reasoning.effort`
    and on Anthropic's `messages` it is `output_config.effort`; an explicit object of either
    name in the agent's params is the more specific statement and keeps its own effort.
    """
    if "reasoning" not in deferred:
        return
    effort = deferred["reasoning"]
    out = result.request_fields
    target = _ROW_FOR_PARAM["reasoning_effort"].fields[endpoint]
    if target is None or "." not in target:
        out[str(target)] = effort
        return
    parent, leaf = target.split(".", 1)
    config = dict(out.get(parent) or {})
    if leaf in config:
        result.notes.append(
            {
                "param": "reasoning_effort",
                "note": f"not applied: the agent sets {target} explicitly, which wins",
            }
        )
    config.setdefault(leaf, effort)
    out[parent] = config


def _land_output_cap(endpoint: str, deferred: dict[str, Any], result: Translation) -> None:
    """Every output-cap spelling -> the one the endpoint accepts.

    `/v1/responses` spells it `max_output_tokens` and the OpenAI SDK raises TypeError for the
    chat spellings before a request is ever sent; Anthropic's `messages` requires `max_tokens`.
    Without this, `endpoint_routing` — the one repair for the documented gpt-5.5+/gpt-5.6
    "function tools ... in /v1/chat/completions" 400 — is unusable for any agent that caps its
    output, which is most of them.

    Translation carries the value VERBATIM. It can lower what reaches the model (an explicit
    native cap wins even when it is smaller) but it never raises one: no path here takes a
    maximum, and no default is invented for an agent that set a cap.
    """
    caps = deferred.get("output_cap")
    if not caps:
        return
    out = result.request_fields
    target = _ROW_FOR_PARAM["max_tokens"].fields[endpoint]
    if target == "*":
        # chat/completions: both spellings are live there; keep whichever the agent wrote.
        out.update(caps)
        return
    if target in caps:
        chosen, source = caps[target], target
    elif "max_completion_tokens" in caps:
        chosen, source = caps["max_completion_tokens"], "max_completion_tokens"
    else:
        chosen, source = next(iter(caps.items()))[1], next(iter(caps))
    for name in caps:
        if name != target:
            result.notes.append(
                {
                    "param": name,
                    "note": (
                        f"translated to {target!r} for the {endpoint} endpoint"
                        if name == source
                        else f"superseded by {source!r}; not sent"
                    ),
                }
            )
    out[target] = copy.deepcopy(chosen)


def _land_sampling(endpoint: str, deferred: dict[str, Any], result: Translation) -> None:
    """Put temperature/top_p/top_k where the target will actually take them.

    On the OpenAI endpoints they are ordinary top-level fields and are forwarded even when the
    model is a reasoning model that answers `Unsupported parameter: 'temperature' is not
    supported with this model.` — dropping one here would hide the very break upshift exists
    to find, so the API answers and a note says the value was sent as declared.

    On Anthropic's `messages`, `anthropic` >= 1.1.0 dropped them from `Messages.create()`, so a
    top-level value raises TypeError in process and the request never reaches the wire —
    identically on both models of an upgrade pair, which leaves the signature undecidable.
    Routed through `extra_body` (the SDK's documented escape hatch) the value is sent as the
    same JSON body field it always was and the API decides. On an SDK that still lists them
    nothing moves. This runs while the request is being BUILT, not in the provider, because
    the request dict is what the recorder writes: a record must show each param where it was
    really sent. An `extra_body` the agent wrote by hand wins, the same rule
    `output_config.effort` follows.
    """
    sampling = deferred.get("sampling")
    if not sampling:
        return
    out = result.request_fields
    if endpoint != MESSAGES:
        out.update(sampling)
        for name in sampling:
            result.notes.append(
                {
                    "param": name,
                    "note": (
                        "sent as declared, not dropped: a model that does not support it "
                        "answers for itself and that answer is the evidence"
                    ),
                }
            )
        return
    extra_body = dict(out.get("extra_body") or {})
    for key, value in sampling.items():
        if messages_create_accepts(key):
            out[key] = value
            continue
        if key in extra_body:
            result.notes.append(
                {"param": key, "note": "not applied: the agent's own extra_body value wins"}
            )
        else:
            result.notes.append(
                {
                    "param": key,
                    "note": (
                        "routed into extra_body: the installed anthropic SDK does not accept "
                        "it as a keyword, and the wire field is unchanged"
                    ),
                }
            )
        extra_body.setdefault(key, value)
    if extra_body:
        out["extra_body"] = extra_body


def _inspect_extra_body(endpoint: str, out: dict[str, Any], result: Translation) -> None:
    """Report what a hand-written `extra_body` carries, without touching it.

    `extra_body` goes to the wire as raw JSON, so a sampling param or a state-linking id
    hidden in it is every bit as real as a top-level one — and invisible to a reader of the
    record unless it is named. It is the agent's own more-specific statement, so it is
    reported and never rewritten.
    """
    extra_body = out.get("extra_body")
    if not isinstance(extra_body, dict):
        return
    for key in extra_body:
        row = _ROW_FOR_PARAM.get(key)
        if row is None:
            continue
        if row.fields.get(endpoint, "") is None:
            result.notes.append(
                {
                    "param": f"extra_body.{key}",
                    "note": (
                        f"sent verbatim in the agent's own extra_body, though {key!r} is "
                        f"dropped from params on this endpoint: {row.reason}"
                    ),
                }
            )
        elif row.name == "sampling":
            result.notes.append(
                {
                    "param": f"extra_body.{key}",
                    "note": (
                        "sampling param sent in the agent's own extra_body; the API answers "
                        "for a model that rejects it"
                    ),
                }
            )


def _responses_tool_choice(value: Any) -> Any:
    """Responses-shaped tool_choice, flattening a chat-shaped one on the way.

    `/v1/responses` takes forced tool choice flat — `{"type": "function", "name": X}` —
    just like its flat tool definitions, while chat/completions nests it under `function`.
    An agent written against chat/completions therefore carries the nested shape (it is what
    `ChatOpenAI.bind_tools(..., tool_choice="X")` produces), and routing it to `/v1/responses`
    without this translation returns `400 Missing required parameter: 'tool_choice.name'` —
    on the very repair (endpoint routing) that the gpt-5.6-family break calls for.

    Strings (`"auto"`, `"none"`, `"required"`) are valid on both endpoints and pass through,
    as does anything already flat or unrecognised, so a bad value produces the API's own 400
    rather than a silent rewrite here.
    """
    if isinstance(value, dict) and value.get("type") == "function" and "name" not in value:
        function = value.get("function")
        if isinstance(function, dict) and function.get("name"):
            flattened = {k: v for k, v in value.items() if k != "function"}
            flattened["name"] = function["name"]
            return flattened
    return copy.deepcopy(value)


def _messages_tool_choice(value: Any) -> Any:
    """Anthropic-shaped tool_choice, translating an OpenAI-shaped value on the way.

    `"required"` -> `{"type": "any"}`, `"auto"`/`"none"` -> `{"type": ...}`,
    `{"type": "function", "function": {"name": X}}` -> `{"type": "tool", "name": X}`.
    Anything already Anthropic-shaped (or unrecognised) passes through untouched, so a bad
    value produces the API's own 400 rather than a silent rewrite here.
    """
    if isinstance(value, str):
        mapped = {"required": "any", "auto": "auto", "none": "none", "any": "any"}.get(value)
        return {"type": mapped} if mapped else value
    if isinstance(value, dict) and value.get("type") == "function":
        function = value.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {"type": "tool", "name": function["name"]}
    return copy.deepcopy(value)


#: The tool_choice row of the translation matrix: one translator per endpoint. chat is the
#: identity because the canonical shape IS the chat shape (upshift's agents are written
#: against chat/completions and routed outward from there).
_TOOL_CHOICE_TRANSLATORS = {
    CHAT: copy.deepcopy,
    RESPONSES: _responses_tool_choice,
    MESSAGES: _messages_tool_choice,
}


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """chat-style nested tool defs -> responses-style flat tool defs."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict):
            converted.append(
                {
                    "type": "function",
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        else:
            converted.append(copy.deepcopy(tool))
    return converted


def convert_tools_messages(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """chat-style nested tool defs -> Anthropic `{name, description, input_schema}`."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict):
            converted.append(
                {
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "input_schema": copy.deepcopy(fn.get("parameters", {})),
                }
            )
        else:
            converted.append(copy.deepcopy(tool))
    return converted


def append_volatile_suffix(
    endpoint: str, items: list[dict[str, Any]], suffix: str
) -> list[dict[str, Any]]:
    """Append a per-request volatile block to the trailing user turn.

    Some agents rebuild a live-facts block on every single request and hang it off the last
    user turn (rescue-ops `cases/A-015/REPORT.md` §4 describes exactly this: a trailing
    user-role message carrying `current_time` and friends, regenerated per request). Without
    it, the eval measures an agent with no clock — a different agent than the one that runs
    in production.

    It goes on the LAST user message when there is one. On `messages` that keeps the
    documented block ordering intact: a tool-result turn's `tool_result` blocks stay first and
    this lands after them. Applied here, at request-building time, so it is never stored in
    the conversation history and never accumulates as the episode grows. Returns a new list.
    """
    if not suffix:
        return items
    out = copy.deepcopy(items)
    last = out[-1] if out else None
    if endpoint == MESSAGES and isinstance(last, dict) and last.get("role") == "user":
        content = last.get("content")
        blocks: list[Any] = (
            [{"type": "text", "text": content}] if isinstance(content, str)
            else (list(content) if isinstance(content, list) else [])
        )
        blocks.append({"type": "text", "text": suffix})
        last["content"] = blocks
        return out
    out.append({"role": "user", "content": suffix})
    return out


def build_request(
    endpoint: str,
    model: str,
    params: dict[str, Any],
    tools: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    system: str | None = None,
    volatile_suffix: str = "",
) -> dict[str, Any]:
    """The request body only. `build_request_with_translation` also returns what the
    translation matrix did to the params, which is what the run record needs."""
    return build_request_with_translation(
        endpoint, model, params, tools, items, system=system, volatile_suffix=volatile_suffix
    )[0]


def build_request_with_translation(
    endpoint: str,
    model: str,
    params: dict[str, Any],
    tools: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    system: str | None = None,
    volatile_suffix: str = "",
) -> tuple[dict[str, Any], Translation]:
    """`system` is only used by the `messages` endpoint, where the system prompt is a
    top-level request field instead of a conversation item.

    Raises TranslationError when the agent's params cannot be carried to `endpoint`
    unchanged; the caller records that as a harness failure, not a model result."""
    items = append_volatile_suffix(endpoint, items, volatile_suffix)
    if endpoint == CHAT:
        request: dict[str, Any] = {
            "model": model,
            "messages": copy.deepcopy(items),
            "tools": copy.deepcopy(tools or []),
        }
    elif endpoint == RESPONSES:
        request = {
            "model": model,
            "input": copy.deepcopy(items),
            "tools": convert_tools(tools or []),
            "store": False,
        }
    elif endpoint == MESSAGES:
        # Anthropic caches only what is marked, and marks are placed on the LAST element of
        # the prefix they close over. Two breakpoints: one on the final tool definition (so
        # the tools array is cached) and one on the system block (tools + system). Nothing is
        # ever marked on `messages`, so the cached prefix is the part that never changes
        # within an episode and the marks never move.
        request = {"model": model, "max_tokens": _max_tokens(params)}
        if system:
            request["system"] = [
                {"type": "text", "text": system, "cache_control": dict(EPHEMERAL)}
            ]
        # An empty system prompt is omitted entirely rather than sent as "" — the claudette
        # adapter has a 0-byte prompt, and an empty block list is not a valid `system`.
        request["messages"] = copy.deepcopy(items)
        request["tools"] = _mark_last_tool_cacheable(convert_tools_messages(tools or []))
    else:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    translation = translate_params(endpoint, params)
    request.update(translation.request_fields)
    return request, translation


def _mark_last_tool_cacheable(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put a cache breakpoint on the last tool definition, so the whole tools array joins the
    cached prefix. No-op for an empty tools list."""
    if tools:
        tools[-1]["cache_control"] = dict(EPHEMERAL)
    return tools


def _max_tokens(params: dict[str, Any]) -> int:
    value = (params or {}).get("max_tokens")
    return value if isinstance(value, int) and value > 0 else DEFAULT_MAX_TOKENS


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass
class _ParsedTurn:
    tool_calls: list[dict[str, Any]]  # [{"id", "name", "arguments": <json str>|dict}]
    text: str
    assistant_items: list[dict[str, Any]]  # conversation items to append for a tool turn
    #: conversation items for a TEXT turn, when the wire format needs more than the plain
    #: text back (Anthropic: the full content block list, thinking blocks included). Empty
    #: for chat_completions/responses, where the loop appends {"role": "assistant", ...}.
    text_items: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None


def parse_response(endpoint: str, response: dict[str, Any]) -> _ParsedTurn:
    if endpoint == CHAT:
        return _parse_chat_response(response)
    if endpoint == MESSAGES:
        return _parse_messages_response(response)
    return _parse_responses_response(response)


def _parse_chat_response(response: dict[str, Any]) -> _ParsedTurn:
    choices = response.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    raw_calls = message.get("tool_calls") or []
    calls = []
    for raw in raw_calls:
        fn = raw.get("function") or {}
        calls.append(
            {"id": raw.get("id"), "name": fn.get("name"), "arguments": fn.get("arguments")}
        )
    if calls:
        return _ParsedTurn(tool_calls=calls, text="", assistant_items=[copy.deepcopy(message)])
    content = message.get("content")
    return _ParsedTurn(
        tool_calls=[], text=content if isinstance(content, str) else "", assistant_items=[]
    )


def _parse_responses_response(response: dict[str, Any]) -> _ParsedTurn:
    output = response.get("output") or []
    calls: list[dict[str, Any]] = []
    assistant_items: list[dict[str, Any]] = []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            calls.append(
                {
                    "id": item.get("call_id"),
                    "name": item.get("name"),
                    "arguments": item.get("arguments"),
                }
            )
            assistant_items.append(
                {
                    "type": "function_call",
                    "call_id": item.get("call_id"),
                    "name": item.get("name"),
                    "arguments": item.get("arguments"),
                }
            )
        elif kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        # unknown item types (e.g. "reasoning") are ignored on purpose
    if calls:
        return _ParsedTurn(tool_calls=calls, text="", assistant_items=assistant_items)
    return _ParsedTurn(tool_calls=[], text="".join(chunks), assistant_items=[])


def _parse_messages_response(response: dict[str, Any]) -> _ParsedTurn:
    """Anthropic content blocks -> calls/text. The assistant turn is replayed with its FULL
    block list (thinking, redacted_thinking, text, tool_use) exactly as received: signatures
    on thinking blocks are only valid against the unmodified sequence."""
    blocks = response.get("content") or []
    stop_reason = response.get("stop_reason")
    calls: list[dict[str, Any]] = []
    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_use":
            calls.append(
                {"id": block.get("id"), "name": block.get("name"), "arguments": block.get("input")}
            )
        elif kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
        # thinking / redacted_thinking / anything else: replayed verbatim, never interpreted
    assistant_turn = {"role": "assistant", "content": copy.deepcopy(blocks)}
    if calls:
        return _ParsedTurn(
            tool_calls=calls, text="", assistant_items=[assistant_turn], stop_reason=stop_reason
        )
    return _ParsedTurn(
        tool_calls=[],
        text="".join(chunks),
        assistant_items=[],
        text_items=[assistant_turn],
        stop_reason=stop_reason,
    )


def _tool_result_item(endpoint: str, call_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(result)
    if endpoint == CHAT:
        return {"role": "tool", "tool_call_id": call_id, "content": payload}
    if endpoint == MESSAGES:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": payload,
        }
        if isinstance(result, dict) and "error" in result:
            block["is_error"] = True
        return block
    return {"type": "function_call_output", "call_id": call_id, "output": payload}


def _append_tool_results(
    endpoint: str, items: list[dict[str, Any]], results: list[tuple[Any, dict[str, Any]]]
) -> None:
    """Anthropic wants every tool_result of a turn in ONE user message, blocks first; the
    OpenAI endpoints want one conversation item per call."""
    if endpoint == MESSAGES:
        blocks = [_tool_result_item(endpoint, call_id, result) for call_id, result in results]
        if blocks:
            items.append({"role": "user", "content": blocks})
        return
    for call_id, result in results:
        items.append(_tool_result_item(endpoint, call_id, result))


def _accumulate_usage(total: dict[str, int], endpoint: str, response: dict[str, Any]) -> None:
    usage = response.get("usage") or {}
    if endpoint == MESSAGES:
        # Anthropic reports `input_tokens` EXCLUDING cache reads, while OpenAI's prompt_tokens
        # includes them and pricing.py treats cached_input_tokens as a subset of input_tokens.
        # Fold cache reads in so both providers record the same thing: total billable input,
        # of which `cached_input_tokens` were served from cache. (Before cache_control was
        # sent, cache_read was always 0, so no previously recorded number changes.)
        cache_read = _as_int(usage.get("cache_read_input_tokens"))
        total["input_tokens"] += _as_int(usage.get("input_tokens")) + cache_read
        total["output_tokens"] += _as_int(usage.get("output_tokens"))
        total["cached_input_tokens"] += cache_read
        # Cache WRITES are billed at their own rate (1.25x input) and are not part of the
        # input total, so they are kept as a separate field.
        total["cache_creation_input_tokens"] = total.get(
            "cache_creation_input_tokens", 0
        ) + _as_int(usage.get("cache_creation_input_tokens"))
        return
    if endpoint == CHAT:
        in_key, out_key, details_key = "prompt_tokens", "completion_tokens", "prompt_tokens_details"
    else:
        in_key, out_key, details_key = "input_tokens", "output_tokens", "input_tokens_details"
    total["input_tokens"] += _as_int(usage.get(in_key))
    total["output_tokens"] += _as_int(usage.get(out_key))
    # Cached input is billed at a deep discount; record it so cost accounting is exact.
    details = usage.get(details_key) or {}
    total["cached_input_tokens"] += _as_int(details.get("cached_tokens"))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# The episode loop
# ---------------------------------------------------------------------------


def run_episode(
    config: AgentConfig,
    case: Case,
    provider: Any,
    backend: Any,
    *,
    rep: int,
    seed: int,
    model_override: str | None = None,
    params_override: dict[str, Any] | None = None,
    endpoint_override: str | None = None,
) -> EpisodeResult:
    """Execute one episode. `backend` must expose .execute(name, arguments) -> dict (never
    raising) and .state() -> dict."""
    endpoint = endpoint_override if endpoint_override is not None else config.endpoint
    model = model_override if model_override is not None else config.model
    params = params_override if params_override is not None else config.params
    if endpoint not in (CHAT, RESPONSES, MESSAGES):
        raise ValueError(f"unknown endpoint {endpoint!r}")

    result = EpisodeResult()
    started = time.monotonic()

    # `messages` carries the system prompt as a top-level request field, not as a turn.
    items: list[dict[str, Any]] = (
        [] if endpoint == MESSAGES else [{"role": "system", "content": config.system_prompt}]
    )
    segments = list(case.user_messages or [])
    if not segments:
        result.final_state = backend.state()
        result.latency_s = time.monotonic() - started
        return result
    items.append({"role": "user", "content": segments[0]})

    segment = 0
    call_idx = 0
    terminal_tools = frozenset(getattr(config, "terminal_tools", ()) or ())
    turn_params = list(getattr(config, "turn_params", ()) or ())
    sim_context = {"case_id": case.id, "rep": rep, "sim": case.sim}

    while call_idx < config.max_turns:
        # DESIGN.md §F. Inert unless agent.json carries `recorded_turns` (capture-derived
        # agents only); all of the policy lives in capture/continuation.py.
        exhausted = continuation.before_turn(config, result, call_idx)
        if exhausted is not None:
            result.api_error = exhausted
            break
        try:
            request, translation = build_request_with_translation(
                endpoint,
                model,
                params_for_turn(params, turn_params, call_idx),
                config.tools,
                items,
                system=config.system_prompt,
                volatile_suffix=getattr(config, "volatile_suffix", ""),
            )
        except TranslationError as exc:
            # A config that cannot be carried to this endpoint unchanged. It is a HARNESS
            # failure, not a model result: no request was built, nothing was sent, and both
            # models of the pair would fail it identically. Recorded so the run says which
            # param and why, and classified by the differ as `harness_error`.
            info = {
                "status_code": None,
                "message": str(exc),
                "type": "translation_error",
                "param": exc.param,
            }
            result.api_calls.append(
                APICall(endpoint=endpoint, request={}, response=None, error=info)
            )
            result.translations.append({})
            result.api_error = info
            break
        result.translations.append(translation.record())
        seed_key = f"{case.id}:{rep}:{call_idx}"
        try:
            response = provider.call(endpoint, request, seed_key, sim_context)
        except ProviderAPIError as exc:
            info = exc.to_dict()
            result.api_calls.append(
                APICall(endpoint=endpoint, request=request, response=None, error=info)
            )
            result.api_error = info
            break

        result.api_calls.append(
            APICall(endpoint=endpoint, request=request, response=response, error=None)
        )
        _accumulate_usage(result.usage, endpoint, response)
        result.resolved_model = response.get("model")

        turn = parse_response(endpoint, response)
        if turn.stop_reason is not None:
            result.stop_reason = turn.stop_reason

        if turn.tool_calls:
            items.extend(copy.deepcopy(turn.assistant_items))
            pending: list[tuple[Any, dict[str, Any]]] = []
            terminal: dict[str, Any] | None = None
            for call in turn.tool_calls:
                name = call.get("name")
                raw_args = call.get("arguments")
                if name in terminal_tools:
                    # The framework stopped here, so the backend is never asked for a result
                    # the framework never had, and nothing is fed back to the model.
                    arguments, tool_result = _decode_arguments(raw_args), dict(
                        TERMINAL_TOOL_RESULT
                    )
                    if terminal is None:
                        terminal = arguments
                else:
                    arguments, tool_result = _execute_call(backend, name, raw_args)
                    pending.append((call.get("id"), tool_result))
                result.tool_executions.append(
                    ToolExecution(
                        turn=call_idx,
                        segment=segment,
                        name=name,
                        arguments=arguments,
                        result=tool_result,
                    )
                )
            call_idx += 1
            if terminal is not None:
                # The episode's output is what the model passed to the terminal tool — the
                # framework's own final answer (pydantic-ai's `.output`, for instance).
                result.final_message = json.dumps(terminal, sort_keys=True)
                break
            _append_tool_results(endpoint, items, pending)
            continue

        # plain text turn: this segment is done
        result.final_message = turn.text
        call_idx += 1
        if segment + 1 < len(segments):
            if turn.text_items:
                items.extend(copy.deepcopy(turn.text_items))
            else:
                items.append({"role": "assistant", "content": turn.text})
            segment += 1
            items.append({"role": "user", "content": segments[segment]})
            continue
        break

    result.final_state = backend.state()
    result.latency_s = time.monotonic() - started
    return result


def _decode_arguments(raw_args: Any) -> dict[str, Any]:
    """Tool-call arguments as a dict. Malformed JSON is kept verbatim under `_raw` rather than
    raised: what the model actually emitted is the evidence."""
    if isinstance(raw_args, dict):
        return raw_args
    text = raw_args if isinstance(raw_args, str) else ""
    if text.strip() == "":
        return {}
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": text}
    return decoded if isinstance(decoded, dict) else {"_raw": text}


def _execute_call(backend: Any, name: Any, raw_args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode arguments and run the tool. Malformed JSON never crashes the episode: nothing is
    executed and the decode error is fed back to the model as the tool result."""
    arguments = _decode_arguments(raw_args)
    if "_raw" in arguments and not isinstance(raw_args, dict):
        return arguments, dict(INVALID_ARGS_ERROR)
    return arguments, backend.execute(name, arguments)
