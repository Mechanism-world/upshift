"""Generic tool-calling executor for one episode (one rep of one case).

Owns message building, request shaping and tool-call parsing for both supported endpoints
(`chat_completions` and `responses`); the provider only performs the transport. See DESIGN.md.

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

INVALID_ARGS_ERROR = {"error": "invalid JSON in tool call arguments"}


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


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def map_params(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Canonical params -> endpoint-specific request fields. Unknown keys pass through."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if endpoint == RESPONSES and key == "reasoning_effort":
            out["reasoning"] = {"effort": value}
        else:
            out[key] = value
    return out


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


def build_request(
    endpoint: str,
    model: str,
    params: dict[str, Any],
    tools: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
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
    else:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    request.update(map_params(endpoint, params))
    return request


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass
class _ParsedTurn:
    tool_calls: list[dict[str, Any]]  # [{"id", "name", "arguments": <json str>}]
    text: str
    assistant_items: list[dict[str, Any]]  # conversation items to append for a tool turn


def parse_response(endpoint: str, response: dict[str, Any]) -> _ParsedTurn:
    if endpoint == CHAT:
        return _parse_chat_response(response)
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


def _tool_result_item(endpoint: str, call_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(result)
    if endpoint == CHAT:
        return {"role": "tool", "tool_call_id": call_id, "content": payload}
    return {"type": "function_call_output", "call_id": call_id, "output": payload}


def _accumulate_usage(total: dict[str, int], endpoint: str, response: dict[str, Any]) -> None:
    usage = response.get("usage") or {}
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
    if endpoint not in (CHAT, RESPONSES):
        raise ValueError(f"unknown endpoint {endpoint!r}")

    result = EpisodeResult()
    started = time.monotonic()

    items: list[dict[str, Any]] = [{"role": "system", "content": config.system_prompt}]
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
        request = build_request(endpoint, model, params, config.tools, items)
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

        if turn.tool_calls:
            items.extend(copy.deepcopy(turn.assistant_items))
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
                items.append(_tool_result_item(endpoint, call.get("id"), tool_result))
            call_idx += 1
            continue

        # plain text turn: this segment is done
        result.final_message = turn.text
        call_idx += 1
        if segment + 1 < len(segments):
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
