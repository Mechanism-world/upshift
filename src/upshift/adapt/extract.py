"""Stage 2: the model as an extraction engine over bounded, cited evidence.

The engine is injectable — `extract(..., call_model=...)` takes any callable that maps a
chat/completions request body to a response dict, so the whole stage is testable offline
with a scripted extractor. Production wires it to the normal provider machinery (see
`adapt/record.py`), which records every attempt under `runs/adapt-<name>/`.

Everything the model returns is a *claim*. Claims carry `file:line` citations and are
checked mechanically in `verify.py`; nothing here trusts them.

Output schema (strict JSON, one object)::

    {
      "agent_name": str,
      "endpoint":  Claim(value: "chat_completions"|"responses"),
      "model":     Claim(value: str|null),
      "params":    Claim(value: {str: scalar}),
      "max_turns": Claim(value: int|null),
      "system_prompt": {
         "status": "found"|"inferred"|"undetermined",
         "note": str,
         "chunks": [{"text": str, "kind": "verbatim"|"templated"|"inferred",
                     "citation": "path:line", "note": str}]
      },
      "tools": [{"name": str, "description": str, "parameters": {json schema},
                 "kind": "verbatim"|"templated"|"inferred", "citation": "path:line",
                 "backend": {"kind": "lookup"|"list"|"create"|"update"|"file_read"|
                                     "file_write"|"unclear",
                             "state_key": str, "id_field": str, "id_prefix": str,
                             "match_fields": [str], "text_field": str,
                             "citation": str, "reason": str}}],
      "cases": [{"id": str, "description": str, "user_messages": [str],
                 "initial_state": {...}, "expected_tool_calls": [{"name", "arguments"}],
                 "final_message": str, "checks": [{"type": ...}],
                 "citation": "path:line", "note": str}],
      "undetermined": [{"what": str, "pointer": str, "why": str}],
      "notes": str
    }

    Claim = {"value": ..., "citation": "path:line", "status": "found"|"inferred"|
             "undetermined", "note": str}
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from upshift.adapt.inventory import Inventory, render_evidence

CallModel = Callable[[dict[str, Any]], dict[str, Any]]

ENDPOINT_VALUES = ("chat_completions", "responses")
CLAIM_STATUSES = ("found", "inferred", "undetermined")
CHUNK_KINDS = ("verbatim", "templated", "inferred")
BACKEND_KINDS = ("lookup", "list", "create", "update", "file_read", "file_write", "unclear")

#: Check types a generated case may use (ADAPTER.md). Anything else is dropped by generate.py.
ALLOWED_CHECK_TYPES = (
    "no_api_error",
    "tool_called",
    "tool_not_called",
    "state_count",
    "final_state",
    "no_tool_calls_after_success",
    "confirmation_id_valid",
    "response_contains",
    "response_not_contains",
    "response_matches",
)

MAX_CASES = 6
MAX_TOOLS = 12

SYSTEM_PROMPT = """\
You are an extraction engine. You read source code of an existing LLM agent and report,
as strict JSON, only what that source actually says.

Hard rules, in priority order:
1. NEVER invent a system prompt, a tool schema, a model name, a parameter or an eval case
   that is not traceable to a line of the evidence you were given.
2. Cite everything. Every claim carries "citation": "<path>:<line>" using the exact paths
   and line numbers shown in the evidence headers and gutters.
3. When the evidence does not settle something, say so: set "status": "undetermined" (or
   omit the item entirely) and add an entry to "undetermined" with a pointer to the file
   that would answer it. An honest hole is a correct answer; a plausible guess is a bug.
4. Mark text "verbatim" ONLY when the exact characters appear in the cited file. Text you
   assembled from a template, or filled placeholders in, is "templated". Text you wrote is
   "inferred". Verbatim claims are checked mechanically against the file; a false verbatim
   claim is the single worst failure mode of this task.
5. Output one JSON object and nothing else. No markdown fence, no commentary.
"""

INSTRUCTIONS = """\
Extract an upshift agent definition from the evidence below.

Target shape (upshift runs plain OpenAI tool-calling agents: one system prompt, a list of
function tools, a loop over /v1/chat/completions or /v1/responses):

{schema}

Field notes:
- endpoint: which API the agent's model call uses. `client.chat.completions.create` and
  `litellm.completion` are "chat_completions"; `client.responses.create` is "responses".
- model / params: what the call site passes (model string, temperature, reasoning_effort,
  tool_choice, ...). Report only request parameters, never SDK/client construction args,
  never `stream`, `messages`, `input` or `tools`.
- system_prompt.chunks: the pieces that make up the system message, in the order they are
  concatenated; they are joined with a single newline. Prefer one chunk per source literal —
  a chunk is only "verbatim" if its exact characters are in one file, so a prompt built by
  concatenating three literals is three verbatim chunks, not one. If the prompt is a
  template with placeholders that the code fills from config with a known default, use kind
  "templated" and explain the substitution in "note".
- tools: OpenAI function schemas. If the code builds them from decorators, pydantic models
  or dicts, reconstruct the resulting schema and mark kind "templated" with the citation of
  the definition. `parameters` must be a JSON Schema object.
- tools[].backend: how the tool's effect could be re-implemented deterministically in
  memory. Use kind "unclear" unless the semantics are mechanical from the code:
    lookup   - reads entries from state[state_key] filtered by match_fields
    list     - returns everything in state[state_key]
    create   - appends one entry to state[state_key], issuing id_prefix + counter
    update   - changes fields of the entry in state[state_key] identified by id_field
    file_read / file_write - reads or writes text files held in state["files"], where
                             match_fields[0] names the path argument and text_field names
                             the content argument
  Anything that talks to a network, a real shell, a database or the clock is "unclear".
- cases: eval scenarios grounded in evidence you can cite — a test, an example script, a
  README usage block. 3 to 6 good cases beat a dozen invented ones; emit zero rather than
  invent. expected_tool_calls is the call sequence the agent should make; checks use only
  these types: {check_types}. Check parameter names are exact and there are no others:
  response_contains/response_not_contains take "text", response_matches takes "regex",
  final_state takes "path" and "equals", state_count takes "path"/"equals"/optional
  "where", tool_called takes "name" plus optional "args_subset"/"exact_args"/"min_times"/
  "max_times", tool_not_called and no_tool_calls_after_success take "name".

{hint}Evidence follows. Line numbers are in the gutter; use them for citations.

{evidence}
"""

SCHEMA_SKELETON = """\
{
  "agent_name": "string, a slug",
  "endpoint": {"value": "chat_completions|responses", "citation": "path:line",
               "status": "found|inferred|undetermined", "note": ""},
  "model": {"value": "string|null", "citation": "", "status": "", "note": ""},
  "params": {"value": {"temperature": 0.7}, "citation": "", "status": "", "note": ""},
  "max_turns": {"value": 6, "citation": "", "status": "", "note": ""},
  "system_prompt": {"status": "found|inferred|undetermined", "note": "",
    "chunks": [{"text": "", "kind": "verbatim|templated|inferred",
                "citation": "path:line", "note": ""}]},
  "tools": [{"name": "", "description": "", "parameters": {"type": "object",
              "properties": {}, "required": []},
             "kind": "verbatim|templated|inferred", "citation": "path:line",
             "backend": {"kind": "lookup|list|create|update|file_read|file_write|unclear",
                         "state_key": "", "id_field": "", "id_prefix": "",
                         "match_fields": [], "text_field": "", "citation": "",
                         "reason": "why this is or is not mechanical"}}],
  "cases": [{"id": "slug", "description": "", "user_messages": [""],
             "initial_state": {}, "expected_tool_calls": [{"name": "", "arguments": {}}],
             "final_message": "", "checks": [{"type": "no_api_error"}],
             "citation": "path:line", "note": ""}],
  "undetermined": [{"what": "", "pointer": "path:line or path", "why": ""}],
  "notes": ""
}"""

RETRY_PREFIX = """\
Your previous reply did not satisfy the schema. Problems:

{errors}

Reply again with one corrected JSON object and nothing else. Do not add claims to fix a
validation error — dropping an item or marking it undetermined is always allowed.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    index: int
    request: dict[str, Any]
    response: dict[str, Any] | None
    text: str
    errors: list[str]
    ok: bool


@dataclass
class ExtractionResult:
    data: dict[str, Any]
    attempts: list[Attempt] = field(default_factory=list)
    ok: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def usage(self) -> dict[str, int]:
        total = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
        for attempt in self.attempts:
            usage = (attempt.response or {}).get("usage") or {}
            total["input_tokens"] += _int(usage.get("prompt_tokens", usage.get("input_tokens")))
            total["output_tokens"] += _int(
                usage.get("completion_tokens", usage.get("output_tokens"))
            )
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            total["cached_input_tokens"] += _int(details.get("cached_tokens"))
        return total


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_messages(inventory: Inventory, agent_hint: str | None = None) -> list[dict[str, str]]:
    hint = f"Operator hint about this agent: {agent_hint.strip()}\n\n" if agent_hint else ""
    user = INSTRUCTIONS.format(
        schema=SCHEMA_SKELETON,
        check_types=", ".join(ALLOWED_CHECK_TYPES),
        hint=hint,
        evidence=render_evidence(inventory),
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_request(
    messages: list[dict[str, str]], model: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    request.update(params or {})
    return request


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def response_text(response: dict[str, Any]) -> str:
    """Assistant text out of either endpoint's response shape."""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    chunks: list[str] = []
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "".join(chunks)


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Tolerant of a stray code fence or trailing prose; strict about the result being one
    JSON object."""
    stripped = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None, "reply contained no JSON object"
        try:
            data = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            return None, f"reply was not valid JSON ({exc.msg} at char {exc.pos})"
    if not isinstance(data, dict):
        return None, f"expected a JSON object, got {type(data).__name__}"
    return data, None


# ---------------------------------------------------------------------------
# Validation (hand-written; no jsonschema dependency)
# ---------------------------------------------------------------------------

CITATION_RE = re.compile(r"^[^\s:][^:]*:\d+(?:-\d+)?$")


def _claim_errors(data: dict[str, Any], key: str, kinds: tuple[type, ...]) -> list[str]:
    claim = data.get(key)
    if claim is None:
        return [f"{key}: missing"]
    if not isinstance(claim, dict):
        return [f"{key}: expected an object with value/citation/status"]
    status = claim.get("status")
    if status not in CLAIM_STATUSES:
        return [f"{key}.status: expected one of {list(CLAIM_STATUSES)}, got {status!r}"]
    value = claim.get("value")
    errors: list[str] = []
    if status != "undetermined" and value is not None and not isinstance(value, kinds):
        names = "/".join(k.__name__ for k in kinds)
        errors.append(f"{key}.value: expected {names}, got {type(value).__name__}")
    return errors


def validate_extraction(data: dict[str, Any]) -> list[str]:
    """Every schema violation, as human sentences. Empty list means the reply is usable."""
    errors: list[str] = []
    if not isinstance(data.get("agent_name"), str) or not data["agent_name"].strip():
        errors.append("agent_name: missing or empty")

    errors += _claim_errors(data, "endpoint", (str,))
    raw_endpoint = data.get("endpoint")
    endpoint = raw_endpoint.get("value") if isinstance(raw_endpoint, dict) else None
    if isinstance(endpoint, str) and endpoint not in (*ENDPOINT_VALUES, "undetermined"):
        errors.append(f"endpoint.value: expected one of {list(ENDPOINT_VALUES)}, got {endpoint!r}")
    errors += _claim_errors(data, "model", (str,))
    errors += _claim_errors(data, "params", (dict,))
    errors += _claim_errors(data, "max_turns", (int,))

    prompt = data.get("system_prompt")
    if not isinstance(prompt, dict):
        errors.append("system_prompt: missing")
    else:
        if prompt.get("status") not in CLAIM_STATUSES:
            errors.append("system_prompt.status: expected found/inferred/undetermined")
        chunks = prompt.get("chunks")
        if not isinstance(chunks, list):
            errors.append("system_prompt.chunks: expected a list")
        else:
            for index, chunk in enumerate(chunks):
                where = f"system_prompt.chunks[{index}]"
                if not isinstance(chunk, dict):
                    errors.append(f"{where}: expected an object")
                    continue
                if not isinstance(chunk.get("text"), str) or not chunk["text"]:
                    errors.append(f"{where}.text: missing or empty")
                if chunk.get("kind") not in CHUNK_KINDS:
                    errors.append(f"{where}.kind: expected one of {list(CHUNK_KINDS)}")
                if not CITATION_RE.match(str(chunk.get("citation", ""))):
                    errors.append(f"{where}.citation: expected 'path:line', got "
                                  f"{chunk.get('citation')!r}")

    tools = data.get("tools")
    if not isinstance(tools, list):
        errors.append("tools: expected a list (use [] and an 'undetermined' entry if none)")
    else:
        seen: set[str] = set()
        for index, tool in enumerate(tools):
            where = f"tools[{index}]"
            if not isinstance(tool, dict):
                errors.append(f"{where}: expected an object")
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{where}.name: missing")
            elif name in seen:
                errors.append(f"{where}.name: duplicate tool name {name!r}")
            else:
                seen.add(name)
            if tool.get("kind") not in CHUNK_KINDS:
                errors.append(f"{where}.kind: expected one of {list(CHUNK_KINDS)}")
            if not CITATION_RE.match(str(tool.get("citation", ""))):
                errors.append(f"{where}.citation: expected 'path:line'")
            params = tool.get("parameters")
            if not isinstance(params, dict):
                errors.append(f"{where}.parameters: expected a JSON Schema object")
            backend = tool.get("backend")
            if not isinstance(backend, dict):
                errors.append(f"{where}.backend: missing (use kind 'unclear' when unsure)")
            elif backend.get("kind") not in BACKEND_KINDS:
                errors.append(f"{where}.backend.kind: expected one of {list(BACKEND_KINDS)}")

    cases = data.get("cases")
    if not isinstance(cases, list):
        errors.append("cases: expected a list (use [] rather than inventing cases)")
    else:
        ids: set[str] = set()
        for index, case in enumerate(cases):
            where = f"cases[{index}]"
            if not isinstance(case, dict):
                errors.append(f"{where}: expected an object")
                continue
            case_id = case.get("id")
            if not isinstance(case_id, str) or not case_id.strip():
                errors.append(f"{where}.id: missing")
            elif case_id in ids:
                errors.append(f"{where}.id: duplicate case id {case_id!r}")
            else:
                ids.add(case_id)
            messages = case.get("user_messages")
            if not isinstance(messages, list) or not messages:
                errors.append(f"{where}.user_messages: expected a non-empty list of strings")
            elif any(not isinstance(m, str) for m in messages):
                errors.append(f"{where}.user_messages: every entry must be a string")
            if not isinstance(case.get("initial_state", {}), dict):
                errors.append(f"{where}.initial_state: expected an object")
            calls = case.get("expected_tool_calls", [])
            if not isinstance(calls, list):
                errors.append(f"{where}.expected_tool_calls: expected a list")
            else:
                for call_index, call in enumerate(calls):
                    if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                        errors.append(
                            f"{where}.expected_tool_calls[{call_index}]: needs a tool name"
                        )
            checks = case.get("checks", [])
            if not isinstance(checks, list):
                errors.append(f"{where}.checks: expected a list")
            if not CITATION_RE.match(str(case.get("citation", ""))):
                errors.append(f"{where}.citation: expected 'path:line' evidence for this case")

    undetermined = data.get("undetermined")
    if undetermined is not None and not isinstance(undetermined, list):
        errors.append("undetermined: expected a list")
    return errors


# ---------------------------------------------------------------------------
# Normalisation: fill defaults so downstream stages are not defensive everywhere
# ---------------------------------------------------------------------------


def _claim(data: dict[str, Any], key: str, default: Any) -> dict[str, Any]:
    claim = data.get(key)
    if not isinstance(claim, dict):
        return {"value": default, "citation": "", "status": "undetermined", "note": ""}
    return {
        "value": claim.get("value", default),
        "citation": str(claim.get("citation") or ""),
        "status": claim.get("status") if claim.get("status") in CLAIM_STATUSES else "inferred",
        "note": str(claim.get("note") or ""),
    }


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    prompt = data.get("system_prompt") if isinstance(data.get("system_prompt"), dict) else {}
    chunks = [c for c in (prompt.get("chunks") or []) if isinstance(c, dict)]
    tools = [t for t in (data.get("tools") or []) if isinstance(t, dict)][:MAX_TOOLS]
    cases = [c for c in (data.get("cases") or []) if isinstance(c, dict)][:MAX_CASES]
    for tool in tools:
        backend = tool.get("backend")
        tool["backend"] = (
            backend if isinstance(backend, dict) else {"kind": "unclear", "reason": ""}
        )
        tool["backend"].setdefault("kind", "unclear")
    return {
        "agent_name": str(data.get("agent_name") or "adapted-agent").strip(),
        "endpoint": _claim(data, "endpoint", "chat_completions"),
        "model": _claim(data, "model", None),
        "params": _claim(data, "params", {}),
        "max_turns": _claim(data, "max_turns", None),
        "system_prompt": {
            "status": prompt.get("status") if prompt.get("status") in CLAIM_STATUSES
            else "undetermined",
            "note": str(prompt.get("note") or ""),
            "chunks": chunks,
        },
        "tools": tools,
        "cases": cases,
        "undetermined": [u for u in (data.get("undetermined") or []) if isinstance(u, dict)],
        "notes": str(data.get("notes") or ""),
    }


EMPTY_EXTRACTION = {
    "agent_name": "adapted-agent",
    "endpoint": {"value": "chat_completions", "citation": "", "status": "undetermined", "note": ""},
    "model": {"value": None, "citation": "", "status": "undetermined", "note": ""},
    "params": {"value": {}, "citation": "", "status": "undetermined", "note": ""},
    "max_turns": {"value": None, "citation": "", "status": "undetermined", "note": ""},
    "system_prompt": {"status": "undetermined", "note": "", "chunks": []},
    "tools": [],
    "cases": [],
    "undetermined": [],
    "notes": "",
}


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def extract(
    inventory: Inventory,
    *,
    call_model: CallModel,
    model: str = "gpt-5.5",
    agent_hint: str | None = None,
    params: dict[str, Any] | None = None,
) -> ExtractionResult:
    """One extraction pass, plus exactly one retry on a schema violation.

    Both attempts go through `call_model`, so both end up in the run record.
    """
    messages = build_messages(inventory, agent_hint)
    attempts: list[Attempt] = []
    last_errors: list[str] = []

    for index in (1, 2):
        request = build_request(messages, model, params)
        response = call_model(request)
        text = response_text(response)
        data, parse_error = parse_json_object(text)
        errors = [parse_error] if parse_error else validate_extraction(data or {})
        attempts.append(
            Attempt(
                index=index, request=request, response=response, text=text,
                errors=errors, ok=not errors,
            )
        )
        if not errors:
            return ExtractionResult(data=normalize(data or {}), attempts=attempts, ok=True)
        last_errors = errors
        if index == 2:
            break
        # Retry carries the model's own reply plus the exact violations.
        messages = [
            *messages,
            {"role": "assistant", "content": text},
            {"role": "user", "content": RETRY_PREFIX.format(
                errors="\n".join(f"- {e}" for e in errors[:20])
            )},
        ]

    # Two schema violations: keep whatever parsed, downstream treats it as low confidence.
    salvage = None
    for attempt in reversed(attempts):
        parsed, _ = parse_json_object(attempt.text)
        if parsed is not None:
            salvage = parsed
            break
    return ExtractionResult(
        data=normalize(salvage) if salvage else copy.deepcopy(EMPTY_EXTRACTION),
        attempts=attempts,
        ok=False,
        errors=last_errors,
    )
