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

_MISSING = object()


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


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def map_params(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Canonical params -> endpoint-specific request fields. Unknown keys pass through."""
    out: dict[str, Any] = {}
    effort = _MISSING
    for key, value in (params or {}).items():
        if endpoint == RESPONSES and key == "reasoning_effort":
            out["reasoning"] = {"effort": value}
        elif endpoint == MESSAGES and key == "reasoning_effort":
            effort = value  # folded into output_config below, after explicit values land
        elif endpoint == MESSAGES and key == "tool_choice":
            out["tool_choice"] = _messages_tool_choice(value)
        else:
            out[key] = copy.deepcopy(value)
    if effort is not _MISSING:
        # Canonical reasoning_effort -> output_config.effort. An explicit output_config.effort
        # in the agent's params is more specific, so it wins.
        config = dict(out.get("output_config") or {})
        config.setdefault("effort", effort)
        out["output_config"] = config
    return out


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


def build_request(
    endpoint: str,
    model: str,
    params: dict[str, Any],
    tools: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    system: str | None = None,
    volatile_suffix: str | None = None,
) -> dict[str, Any]:
    """`system` is only used by the `messages` endpoint, where the system prompt is a
    top-level request field instead of a conversation item.

    `volatile_suffix` (ADAPTER.md, "Volatile suffix") is the harness-appended trailing message
    some agents send on every request — a live-facts block, a per-request reminder. It is sent
    as ONE user-role message placed after every conversation item, on all three endpoints. It
    is appended to the outgoing request only, never to `items`, so the conversation history
    the loop replays next turn does not accumulate copies of it.
    """
    wire_items = _with_volatile_suffix(items, volatile_suffix)
    if endpoint == CHAT:
        request: dict[str, Any] = {
            "model": model,
            "messages": wire_items,
            "tools": copy.deepcopy(tools or []),
        }
    elif endpoint == RESPONSES:
        request = {
            "model": model,
            "input": wire_items,
            "tools": convert_tools(tools or []),
            "store": False,
        }
    elif endpoint == MESSAGES:
        # Anthropic caches only what is marked, and marks are placed on the LAST element of
        # the prefix they close over. Two breakpoints: one on the final tool definition (so
        # the tools array is cached) and one on the system block (tools + system). Nothing is
        # ever marked on `messages`, so the cached prefix is the part that never changes
        # within an episode and the marks never move. The volatile suffix is the LAST element
        # of `messages`, i.e. after both breakpoints: it is exactly the content Anthropic's
        # caching guidance says to put behind the cached prefix, and it must never be marked
        # (a breakpoint on it would move every turn and cache nothing).
        request = {"model": model, "max_tokens": _max_tokens(params)}
        if system:
            request["system"] = [
                {"type": "text", "text": system, "cache_control": dict(EPHEMERAL)}
            ]
        # An empty system prompt is omitted entirely rather than sent as "" — the claudette
        # adapter has a 0-byte prompt, and an empty block list is not a valid `system`.
        request["messages"] = wire_items
        request["tools"] = _mark_last_tool_cacheable(convert_tools_messages(tools or []))
    else:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    request.update(map_params(endpoint, params))
    return request


def volatile_suffix_item(text: str) -> dict[str, Any]:
    """The wire item for the volatile suffix: a plain user-role text message. The same shape
    is valid on all three endpoints. On `messages` it may follow another user-role message (the
    tool_result turn); Anthropic combines consecutive same-role turns into one, with the
    tool_result blocks first, which is the order the API requires."""
    return {"role": "user", "content": text}


def _with_volatile_suffix(
    items: list[dict[str, Any]], volatile_suffix: str | None
) -> list[dict[str, Any]]:
    """A fresh copy of the conversation with the volatile suffix appended last, or a fresh
    copy of the conversation alone when there is no suffix."""
    wire_items = copy.deepcopy(items)
    if volatile_suffix:
        wire_items.append(volatile_suffix_item(volatile_suffix))
    return wire_items


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
    sim_context = {"case_id": case.id, "rep": rep, "sim": case.sim}

    while call_idx < config.max_turns:
        request = build_request(
            endpoint,
            model,
            params,
            config.tools,
            items,
            system=config.system_prompt,
            volatile_suffix=config.volatile_suffix,
        )
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
            for call in turn.tool_calls:
                name = call.get("name")
                raw_args = call.get("arguments")
                arguments, tool_result = _execute_call(backend, name, raw_args)
                result.tool_executions.append(
                    ToolExecution(
                        turn=call_idx,
                        segment=segment,
                        name=name,
                        arguments=arguments,
                        result=tool_result,
                    )
                )
                pending.append((call.get("id"), tool_result))
            _append_tool_results(endpoint, items, pending)
            call_idx += 1
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


def _execute_call(backend: Any, name: Any, raw_args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode arguments and run the tool. Malformed JSON never crashes the episode: nothing is
    executed and the decode error is fed back to the model as the tool result."""
    if isinstance(raw_args, dict):
        arguments = raw_args
    else:
        text = raw_args if isinstance(raw_args, str) else ""
        if text.strip() == "":
            arguments = {}
        else:
            try:
                decoded = json.loads(text)
            except (ValueError, TypeError):
                return {"_raw": text}, dict(INVALID_ARGS_ERROR)
            if not isinstance(decoded, dict):
                return {"_raw": text}, dict(INVALID_ARGS_ERROR)
            arguments = decoded
    return arguments, backend.execute(name, arguments)
