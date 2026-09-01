"""Deterministic local simulator of two upgrade pairs: `sim-5.5` -> `sim-5.6-sol` (OpenAI
chat_completions/responses) and `sim-fable-5` -> `sim-fable-5-1` (Anthropic `messages`).

It exists so the whole pipeline can be exercised end-to-end with zero API cost. It emits
wire-accurate response dicts for both endpoints, executes each case's `sim.oracle_plan`, and —
for sim-5.6 — reproduces the documented failure modes (hard 400 on chat_completions + tools,
plus seeded behavioral corruptions that switch off when the corresponding repair marker is
present in the prompt or tool schema).

What `sim-fable-*` CAN do: speak the Messages wire shape (system string, message turns with
content blocks, `{name, description, input_schema}` tools, `tool_use`/`tool_result` blocks,
`stop_reason`, Anthropic usage fields); replay the same oracle plans as the OpenAI sim; and,
on `sim-fable-5-1` only, reproduce three documented 5 -> 5.1 changes:

  * forced `tool_choice` (`{"type": "tool"|"any"}`) -> the exact documented 400;
  * `serialize` — a plan step with k>1 tool calls is emitted one call per assistant turn,
    suppressed when the documented batching sentence is in the system prompt;
  * `skip_retrieval` — calls to retrieval tools are dropped while `output_config.effort` is
    absent or below `xhigh`, suppressed by the documented verification nudge or effort
    >= xhigh. Retrieval tools come from `sim.retrieval_tools`, else names matching
    search|retriev|lookup|query|fetch|find.

What it CANNOT do: produce thinking or redacted_thinking blocks, signature validation,
prompt-cache accounting (cache usage fields are always 0), streaming, `thinking_block_invalid`
or the sampling-parameter 400s, partial/`max_tokens` stops, or any judgement about whether a
real Fable would behave this way. The two 5.1 corruptions fire deterministically (rate 1.0)
whenever they apply, so a sim upgrade run is a machinery test, not evidence.

Honesty: the sim validates the MACHINERY, never the thesis. Its response to repairs is true by
construction and proves nothing about real models (DESIGN.md, "Sim provider").

Oracle plan format (`case.sim.oracle_plan`), a list of steps executed in order, one step per
assistant turn::

    [{"tool_calls": [{"name": "search_flights", "arguments": {"origin": "SFO"}}]},
     {"tool_calls": [{"name": "book_flight",
                      "arguments": {"flight_id": "$ref:search_flights[0].results[0].flight_id"}}]},
     {"final_message": "Booked. Confirmation {ref:book_flight[0].confirmation_id}."}]

Accepted aliases: `"message"` / `"text"` for `"final_message"`, `"calls"` for `"tool_calls"`,
`"args"` for `"arguments"`, `"tool"` for `"name"`, and a bare `{"name": ..., "arguments": ...}`
step for a single call.

References: `$ref:<tool>[<i>].<path>` inside argument values and `{ref:<tool>[<i>].<path>}`
inside messages resolve against the i-th result of that tool observed so far in the request
conversation (index defaults to 0). Unresolvable -> "REF-ERROR".

Other knobs read off `case.sim`:
  * `"vulnerable_to": ["duplicate_call", "over_acting", "skip_tool"]` — 5.6 corruptions.
  * `"flaky": {"rate": 0.2, "mode": "skip_final_tool"}` — case-level flakiness, both models.
  * `"critical_tool": "<tool name>"` — which tool the `duplicate_call` and `skip_tool`
    corruptions target. Defaults to the victim's `book_flight`; a foreign agent names its own.
  * `"fabricated_id_prefix": "UPS-9"` — prefix of the identifier `skip_tool` invents; must
    match the case's `confirmation_id_valid` pattern for the fabrication to be detectable.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any

from upshift.providers.base import Provider, ProviderAPIError

CHAT = "chat_completions"
RESPONSES = "responses"
MESSAGES = "messages"

#: model prefix -> the one endpoint that model speaks
MODEL_ENDPOINTS = {"sim-5.5": (CHAT, RESPONSES), "sim-5.6": (CHAT, RESPONSES),
                   "sim-fable-5": (MESSAGES,), "sim-fable-5-1": (MESSAGES,)}

REF_ERROR = "REF-ERROR"
#: Default target of the duplicate_call / skip_tool corruptions (the victim's booking tool).
#: Foreign agents override it per case with `sim.critical_tool`.
BOOK_TOOL = "book_flight"
#: Default prefix of the identifier the skip_tool corruption invents (`sim.fabricated_id_prefix`).
FABRICATED_ID_PREFIX = "UPS-9"
DONE_MESSAGE = "Done."

NO_PLAN_MESSAGE = (
    "sim provider requires sim.oracle_plan per case; use --provider openai for real agents"
)

HARD_BREAK_MESSAGE = (
    "Function tools with reasoning_effort are not supported for sim-5.6-sol in "
    "/v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort "
    "to 'none'."
)

FORCED_TOOL_CHOICE_MESSAGE = (
    'tool_choice: type "tool" and "any" are not supported for this model.'
)

# Corruption probabilities (DESIGN.md "Sim provider").
CORRUPTION_RATES = {"duplicate_call": 0.8, "over_acting": 0.7, "skip_tool": 0.7}
#: The documented 5 -> 5.1 behavior changes: deterministic when they apply.
FABLE_CORRUPTION_RATES = {"serialize": 1.0, "skip_retrieval": 1.0}

#: Effort ladder of the messages endpoint; skip_retrieval stops at xhigh.
EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")
RETRIEVAL_EFFORT_FLOOR = "xhigh"
_RETRIEVAL_NAME_RE = re.compile(r"search|retriev|lookup|query|fetch|find", re.IGNORECASE)

# Exact repair markers. These strings are a contract with repair/playbook.py.
DUPLICATE_PROMPT_MARKERS = ("at most once", "exactly once")
DUPLICATE_TOOL_MARKER = "exactly once"
OVER_ACTING_PROMPT_MARKER = "stop once the task is complete"
SKIP_PROMPT_MARKER = "never state a confirmation number"
SKIP_TOOL_MARKER = "must be called"
SERIALIZE_PROMPT_MARKER = "request every item that doesn't depend on another's result"
RETRIEVAL_PROMPT_MARKER = "search before answering"

_MISSING = object()


# ---------------------------------------------------------------------------
# Determinism helpers
# ---------------------------------------------------------------------------


def _endpoints_for(model: str) -> tuple[str, ...] | None:
    """Endpoints the named sim model speaks, or None when it is not simulated at all."""
    best: tuple[str, tuple[str, ...]] | None = None
    for prefix, endpoints in MODEL_ENDPOINTS.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, endpoints)
    return best[1] if best else None


def _rng(case_id: str, rep: int, model: str, rule_name: str) -> random.Random:
    digest = hashlib.sha256(f"{case_id}:{rep}:{model}:{rule_name}".encode()).hexdigest()
    return random.Random(digest)


def _hash8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Plan normalisation
# ---------------------------------------------------------------------------


def _normalize_plan(plan: Any) -> list[tuple[str, Any]]:
    """-> [("tools", [{"name", "arguments"}, ...]) | ("message", str), ...]"""
    steps: list[tuple[str, Any]] = []
    for raw in plan or []:
        if isinstance(raw, str):
            steps.append(("message", raw))
            continue
        if not isinstance(raw, dict):
            continue
        calls = raw.get("tool_calls", raw.get("calls"))
        if calls is None and (raw.get("tool") or raw.get("name")):
            calls = [raw]
        if calls is not None:
            normalized = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                normalized.append(
                    {
                        "name": call.get("name") or call.get("tool"),
                        "arguments": call.get("arguments", call.get("args", {})) or {},
                    }
                )
            steps.append(("tools", normalized))
            continue
        for key in ("final_message", "message", "text"):
            if raw.get(key) is not None:
                steps.append(("message", str(raw[key])))
                break
    return steps


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[\s*(\d+)\s*\])?\s*\.?\s*(.*?)\s*$")
_MSG_REF_RE = re.compile(r"\{ref:([^}]*)\}")
_PART_RE = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")


def _walk(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not part:
            continue
        match = _PART_RE.match(part)
        if not match:
            return _MISSING
        key, index_blob = match.group(1), match.group(2)
        if key:
            if isinstance(obj, dict) and key in obj:
                obj = obj[key]
            else:
                return _MISSING
        for raw_index in re.findall(r"\[(\d+)\]", index_blob):
            index = int(raw_index)
            if isinstance(obj, list) and index < len(obj):
                obj = obj[index]
            else:
                return _MISSING
    return obj


def _resolve_ref(ref: str, results: dict[str, list[Any]]) -> Any:
    match = _REF_RE.match(ref or "")
    if not match:
        return _MISSING
    name, raw_index, path = match.group(1), match.group(2), match.group(3)
    index = int(raw_index) if raw_index is not None else 0
    entries = results.get(name) or []
    if index >= len(entries):
        return _MISSING
    return _walk(entries[index], path)


def _resolve_arguments(value: Any, results: dict[str, list[Any]]) -> Any:
    if isinstance(value, str):
        if value.startswith("$ref:"):
            resolved = _resolve_ref(value[len("$ref:") :], results)
            return REF_ERROR if resolved is _MISSING else resolved
        return value
    if isinstance(value, dict):
        return {k: _resolve_arguments(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_arguments(v, results) for v in value]
    return value


def _render_message(
    text: str,
    results: dict[str, list[Any]],
    fabricated: str | None = None,
    critical_tool: str = BOOK_TOOL,
) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = match.group(1).strip()
        if fabricated is not None and ref.startswith(critical_tool):
            return fabricated
        value = _resolve_ref(ref, results)
        if value is _MISSING:
            return REF_ERROR
        return value if isinstance(value, str) else json.dumps(value)

    return _MSG_REF_RE.sub(replace, text)


# ---------------------------------------------------------------------------
# Reading the request back (tool results, system prompt, tool schemas)
# ---------------------------------------------------------------------------


def _loads(payload: Any) -> Any:
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return {}
    return payload if payload is not None else {}


def _parse_results(endpoint: str, request: dict[str, Any]) -> dict[str, list[Any]]:
    """Tool results seen so far, in order of appearance, grouped by tool name. Call ids are
    matched against the function calls the sim emitted earlier in the same conversation."""
    results: dict[str, list[Any]] = {}
    id_to_name: dict[Any, Any] = {}
    if endpoint == MESSAGES:
        for message in request.get("messages") or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    id_to_name[block.get("id")] = block.get("name")
                elif block.get("type") == "tool_result":
                    name = id_to_name.get(block.get("tool_use_id"))
                    results.setdefault(name, []).append(_loads(block.get("content")))
        return results
    if endpoint == CHAT:
        for message in request.get("messages") or []:
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    id_to_name[call.get("id")] = fn.get("name")
            if message.get("role") == "tool":
                name = id_to_name.get(message.get("tool_call_id"))
                results.setdefault(name, []).append(_loads(message.get("content")))
    else:
        for item in request.get("input") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                id_to_name[item.get("call_id")] = item.get("name")
            elif item.get("type") == "function_call_output":
                name = id_to_name.get(item.get("call_id"))
                results.setdefault(name, []).append(_loads(item.get("output")))
    return results


def _system_prompt(endpoint: str, request: dict[str, Any]) -> str:
    if endpoint == MESSAGES:
        system = request.get("system")
        if isinstance(system, str):
            return system
        chunks = [
            block.get("text", "")
            for block in system or []
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(chunks)
    items = request.get("messages") if endpoint == CHAT else request.get("input")
    chunks = []
    for item in items or []:
        if isinstance(item, dict) and item.get("role") == "system":
            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
    return "\n".join(chunks)


def _tool_description(request: dict[str, Any], tool_name: str) -> str:
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if fn.get("name") == tool_name:
            description = fn.get("description")
            return description if isinstance(description, str) else ""
    return ""


# ---------------------------------------------------------------------------
# Wire-format response builders
# ---------------------------------------------------------------------------


def _usage_numbers(request: dict[str, Any], output_units: int) -> tuple[int, int]:
    items = request.get("messages") or request.get("input") or []
    return 20 + 7 * len(items), 5 + 3 * output_units


def _chat_tool_response(
    model: str, seed_key: str, request: dict[str, Any], calls: list[tuple[str, Any]]
) -> dict[str, Any]:
    tool_calls = []
    for index, (name, arguments) in enumerate(calls):
        tool_calls.append(
            {
                "id": f"call_{_hash8(f'{seed_key}:{index}')}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        )
    prompt_tokens, completion_tokens = _usage_numbers(request, len(calls))
    return {
        "id": f"sim-{_hash8(seed_key)}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _chat_text_response(
    model: str, seed_key: str, request: dict[str, Any], text: str
) -> dict[str, Any]:
    prompt_tokens, completion_tokens = _usage_numbers(request, max(1, len(text) // 8))
    return {
        "id": f"sim-{_hash8(seed_key)}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _responses_tool_response(
    model: str, seed_key: str, request: dict[str, Any], calls: list[tuple[str, Any]]
) -> dict[str, Any]:
    output = []
    for index, (name, arguments) in enumerate(calls):
        token = _hash8(f"{seed_key}:{index}")
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{token}",
                "call_id": f"call_{token}",
                "name": name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        )
    input_tokens, output_tokens = _usage_numbers(request, len(calls))
    return {
        "id": f"sim-{_hash8(seed_key)}",
        "object": "response",
        "created_at": 0,
        "model": model,
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _responses_text_response(
    model: str, seed_key: str, request: dict[str, Any], text: str
) -> dict[str, Any]:
    input_tokens, output_tokens = _usage_numbers(request, max(1, len(text) // 8))
    return {
        "id": f"sim-{_hash8(seed_key)}",
        "object": "response",
        "created_at": 0,
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": f"msg_{_hash8(seed_key)}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _messages_usage(request: dict[str, Any], output_units: int) -> dict[str, int]:
    input_tokens, output_tokens = _usage_numbers(request, output_units)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _messages_tool_response(
    model: str, seed_key: str, request: dict[str, Any], calls: list[tuple[str, Any]]
) -> dict[str, Any]:
    content = []
    for index, (name, arguments) in enumerate(calls):
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_{_hash8(f'{seed_key}:{index}')}",
                "name": name,
                "input": arguments,
            }
        )
    return {
        "id": f"msg_{_hash8(seed_key)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": _messages_usage(request, len(calls)),
    }


def _messages_text_response(
    model: str, seed_key: str, request: dict[str, Any], text: str
) -> dict[str, Any]:
    return {
        "id": f"msg_{_hash8(seed_key)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": _messages_usage(request, max(1, len(text) // 8)),
    }


# ---------------------------------------------------------------------------
# messages-endpoint helpers
# ---------------------------------------------------------------------------


def _effort(request: dict[str, Any]) -> str | None:
    config = request.get("output_config")
    effort = config.get("effort") if isinstance(config, dict) else None
    return effort if isinstance(effort, str) else None


def _effort_at_least(request: dict[str, Any], floor: str) -> bool:
    effort = _effort(request)
    if effort not in EFFORT_LADDER:
        return False  # absent or unknown: the documented default (high) is below xhigh
    return EFFORT_LADDER.index(effort) >= EFFORT_LADDER.index(floor)


def _forced_tool_choice(request: dict[str, Any]) -> bool:
    choice = request.get("tool_choice")
    return isinstance(choice, dict) and choice.get("type") in ("tool", "any")


def _is_retrieval(name: Any, declared: list[str] | None) -> bool:
    if not isinstance(name, str):
        return False
    if declared is not None:
        return name in declared
    return bool(_RETRIEVAL_NAME_RE.search(name))


# ---------------------------------------------------------------------------
# Per-episode state
# ---------------------------------------------------------------------------


class _EpisodeState:
    """Plan cursor plus the seeded draws for one (case_id, rep, model) episode. Every draw is
    made exactly once, here, at episode start."""

    def __init__(self, case_id: str, rep: int, model: str, sim: dict[str, Any]) -> None:
        self.steps = _normalize_plan(sim.get("oracle_plan"))
        self.cursor = 0
        #: calls the `serialize` corruption deferred to later assistant turns
        self.pending: list[tuple[str, Any]] = []
        declared = sim.get("retrieval_tools")
        self.retrieval_tools = [str(t) for t in declared] if isinstance(declared, list) else None
        self.last_tool_call: tuple[str, Any] | None = None
        self.over_acting_done = False
        self.critical_tool = str(sim.get("critical_tool") or BOOK_TOOL)

        tool_steps = [i for i, (kind, _) in enumerate(self.steps) if kind == "tools"]
        self.last_tool_idx = tool_steps[-1] if tool_steps else None
        message_steps = [i for i, (kind, _) in enumerate(self.steps) if kind == "message"]
        self.final_message_idx = message_steps[-1] if message_steps else None
        self.first_critical_idx = None
        for index, (kind, payload) in enumerate(self.steps):
            if kind == "tools" and any(c["name"] == self.critical_tool for c in payload):
                self.first_critical_idx = index
                break

        flaky = sim.get("flaky") or {}
        self.flaky_skip = False
        if isinstance(flaky, dict) and flaky.get("mode") == "skip_final_tool":
            rate = float(flaky.get("rate") or 0.0)
            self.flaky_skip = _rng(case_id, rep, model, "flaky").random() < rate

        self.draws: dict[str, bool] = {}
        if model.startswith("sim-5.6"):
            vulnerable = sim.get("vulnerable_to") or []
            for rule, rate in CORRUPTION_RATES.items():
                if rule in vulnerable:
                    self.draws[rule] = _rng(case_id, rep, model, rule).random() < rate
        elif model.startswith("sim-fable-5-1"):
            # The documented 5 -> 5.1 changes are not per-case vulnerabilities: they apply to
            # every episode where the shape allows (a step with k>1 calls; a retrieval tool).
            for rule, rate in FABLE_CORRUPTION_RATES.items():
                self.draws[rule] = _rng(case_id, rep, model, rule).random() < rate

        digits = _rng(case_id, rep, model, "fabricate").randrange(1000)
        prefix = str(sim.get("fabricated_id_prefix") or FABRICATED_ID_PREFIX)
        self.fabricated_id = f"{prefix}{digits:03d}"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SimProvider(Provider):
    name = "sim"

    def __init__(self) -> None:
        self._episodes: dict[tuple[str, int, str], _EpisodeState] = {}

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model = request.get("model") or ""
        endpoints = _endpoints_for(model)
        if endpoints is None:
            raise ProviderAPIError(
                message=f"The sim provider does not simulate model {model!r}.",
                status_code=404,
                error_type="model_not_found",
            )
        if endpoint not in (CHAT, RESPONSES, MESSAGES):
            raise ValueError(f"unknown endpoint {endpoint!r}")
        if endpoint not in endpoints:
            raise ProviderAPIError(
                message=(
                    f"model {model!r} is only served on endpoint(s) "
                    f"{', '.join(endpoints)}; got {endpoint!r}."
                ),
                status_code=404,
                error_type="model_not_found",
            )

        context = sim_context or {}
        case_id = str(context.get("case_id", ""))
        rep = int(context.get("rep", 0) or 0)
        sim = context.get("sim") or {}

        # The sim can only replay a plan it was given. Without one it would silently answer
        # "Done." to everything and every case would look like a real regression.
        if not (sim.get("oracle_plan") or []):
            raise ValueError(f"{NO_PLAN_MESSAGE} (case {case_id!r} declares none)")

        key = (case_id, rep, model)
        if seed_key.endswith(":0"):
            self._episodes.pop(key, None)
        state = self._episodes.get(key)
        if state is None:
            state = _EpisodeState(case_id, rep, model, sim)
            self._episodes[key] = state

        is_56 = model.startswith("sim-5.6")
        is_fable_51 = model.startswith("sim-fable-5-1")

        # 1. The documented 5.1 break: forced tool_choice, before any plan logic.
        if is_fable_51 and _forced_tool_choice(request):
            raise ProviderAPIError(
                message=FORCED_TOOL_CHOICE_MESSAGE,
                status_code=400,
                error_type="invalid_request_error",
            )

        # 1b. The documented 5.6 hard break: fires before any plan logic, on every such call.
        if (
            is_56
            and endpoint == CHAT
            and request.get("tools")
            and request.get("reasoning_effort") != "none"
        ):
            raise ProviderAPIError(
                message=HARD_BREAK_MESSAGE,
                status_code=400,
                error_type="invalid_request_error",
            )

        results = _parse_results(endpoint, request)
        prompt = _system_prompt(endpoint, request).lower()
        tool_description = _tool_description(request, state.critical_tool).lower()

        duplicate = state.draws.get("duplicate_call", False) and not (
            any(marker in prompt for marker in DUPLICATE_PROMPT_MARKERS)
            or DUPLICATE_TOOL_MARKER in tool_description
        )
        over_acting = state.draws.get("over_acting", False) and (
            OVER_ACTING_PROMPT_MARKER not in prompt
        )
        skip_tool = state.draws.get("skip_tool", False) and not (
            SKIP_PROMPT_MARKER in prompt or SKIP_TOOL_MARKER in tool_description
        )
        serialize = state.draws.get("serialize", False) and SERIALIZE_PROMPT_MARKER not in prompt
        skip_retrieval = state.draws.get("skip_retrieval", False) and not (
            RETRIEVAL_PROMPT_MARKER in prompt
            or _effort_at_least(request, RETRIEVAL_EFFORT_FLOOR)
        )

        # Calls the previous turn deferred (serialize): one per assistant turn, in order.
        if state.pending:
            call = state.pending.pop(0)
            state.last_tool_call = call
            return self._tools(endpoint, model, seed_key, request, [call])

        # Advance past steps this episode drops.
        while state.cursor < len(state.steps):
            kind, _ = state.steps[state.cursor]
            if kind != "tools":
                break
            if skip_tool and state.cursor == state.first_critical_idx:
                state.cursor += 1
                continue
            step_calls = state.steps[state.cursor][1]
            if skip_retrieval and step_calls and all(
                _is_retrieval(c["name"], state.retrieval_tools) for c in step_calls
            ):
                state.cursor += 1
                continue
            if state.flaky_skip and state.cursor == state.last_tool_idx:
                state.cursor += 1
                continue
            break

        if state.cursor >= len(state.steps):
            return self._text(endpoint, model, seed_key, request, DONE_MESSAGE)

        kind, payload = state.steps[state.cursor]

        if kind == "tools":
            calls: list[tuple[str, Any]] = []
            for call in payload:
                name = call["name"]
                if skip_retrieval and _is_retrieval(name, state.retrieval_tools):
                    continue
                arguments = _resolve_arguments(call["arguments"], results)
                calls.append((name, arguments))
                if (
                    duplicate
                    and state.cursor == state.first_critical_idx
                    and name == state.critical_tool
                ):
                    calls.append((name, arguments))
            state.cursor += 1
            if serialize and len(calls) > 1:
                # 5.1 issues at most one tool call per assistant turn: the rest of this plan
                # step is deferred to the following turns.
                state.pending = calls[1:]
                calls = calls[:1]
            if calls:
                state.last_tool_call = calls[-1]
            return self._tools(endpoint, model, seed_key, request, calls)

        # message step
        if (
            over_acting
            and state.cursor == state.final_message_idx
            and not state.over_acting_done
            and state.last_tool_call is not None
        ):
            state.over_acting_done = True
            return self._tools(endpoint, model, seed_key, request, [state.last_tool_call])

        fabricated = state.fabricated_id if skip_tool else None
        text = _render_message(payload, results, fabricated, state.critical_tool)
        # The invented confirmation is only volunteered at the end of the episode; an
        # intermediate turn keeps whatever the plan said.
        if (
            fabricated is not None
            and state.cursor == state.final_message_idx
            and fabricated not in text
        ):
            text = f"{text} Your confirmation number is {fabricated}."
        state.cursor += 1
        return self._text(endpoint, model, seed_key, request, text)

    # -- emitters ----------------------------------------------------------

    @staticmethod
    def _tools(
        endpoint: str,
        model: str,
        seed_key: str,
        request: dict[str, Any],
        calls: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        if endpoint == CHAT:
            return _chat_tool_response(model, seed_key, request, calls)
        if endpoint == MESSAGES:
            return _messages_tool_response(model, seed_key, request, calls)
        return _responses_tool_response(model, seed_key, request, calls)

    @staticmethod
    def _text(
        endpoint: str, model: str, seed_key: str, request: dict[str, Any], text: str
    ) -> dict[str, Any]:
        if endpoint == CHAT:
            return _chat_text_response(model, seed_key, request, text)
        if endpoint == MESSAGES:
            return _messages_text_response(model, seed_key, request, text)
        return _responses_text_response(model, seed_key, request, text)
