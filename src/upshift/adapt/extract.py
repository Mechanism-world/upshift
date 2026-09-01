"""Stage 2: the model as an extraction engine over bounded, cited evidence.

The engine is injectable — `extract(..., call_model=...)` takes any callable that maps a
chat/completions request body to a response dict, so the whole stage is testable offline
with a scripted extractor. Production wires it to the normal provider machinery (see
`adapt/record.py`), which records every attempt under `runs/adapt-<name>/`.

Everything the model returns is a *claim*. Claims carry `file:line` citations and are
checked mechanically in `verify.py`; nothing here trusts them.

Two rounds, at most. Round 1 reads the statically ranked evidence and is expected to come
back with honest `undetermined` entries whose `pointer` says where the answer lives (tool
schemas hidden in docstrings, a registration site whose handlers are defined elsewhere).
Round 2 follows those pointers: it slices the source they name, hands the model its own
previous JSON plus that new source, and takes the corrected result.

Round 2 runs only when a pointer names source the model has *not read* — a file with no
round-1 slice, or a line range in a ranked file that fell between that file's round-1
windows, in which case only the uncovered part of the window is sent. A pointer at lines
round 1 already showed buys nothing and triggers nothing. Round 2 spends what is left of the
same evidence budget, goes through the same cost guard, and never loops: a failed, skipped or
aborted round 2 leaves round 1 standing.

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
from pathlib import Path
from typing import Any

from upshift.adapt import AdaptAborted
from upshift.adapt.inventory import (
    Inventory,
    Slice,
    estimate_tokens,
    read_text,
    readable_source,
    render_evidence,
    render_slices,
    slice_pointer,
)

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

ROUND2_INSTRUCTIONS = """\
You previously returned the JSON below.

{previous}

The additional source below covers locations you cited as undetermined or could not see.
Produce the complete corrected extraction JSON: resolve what the new evidence settles, keep
prior found claims unless the new evidence contradicts them, keep honest undetermined entries
for what remains unseen.

The schema and the rules are unchanged:

{schema}

Checks may use only these types: {check_types}.

The evidence below is NEW source only — the files you were shown before are not repeated, so
do not drop a claim merely because you cannot see its citation this time. Line numbers are in
the gutter; use them for citations.

{evidence}
"""

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
class Pointer:
    """A claim saying "the answer is over there"."""

    raw: str
    path: str  # repo-relative, posix, resolved to a file that exists
    line: int | None
    source: str  # which claim produced it, for the report


@dataclass
class Round2:
    """What the second extraction round did, or why it did not happen.

    Exactly one of `skipped`, `aborted` and `ran` is meaningful: skipped means no call was
    made, aborted means a call was refused or failed mid-round, ran means the model replied.
    """

    ran: bool = False
    ok: bool = False
    used: bool = False  # did round 2's JSON replace round 1's?
    skipped: str = ""
    aborted: str = ""
    pointers: list[Pointer] = field(default_factory=list)  # every pointer that resolved
    followed: list[Pointer] = field(default_factory=list)  # those that contributed evidence
    files: list[str] = field(default_factory=list)
    slices: list[Slice] = field(default_factory=list)
    evidence_tokens: int = 0
    dropped_slices: int = 0  # slices the remaining budget could not fit
    first_attempt: int = 0  # index of round 2's first attempt in ExtractionResult.attempts
    settled: list[str] = field(default_factory=list)
    resolved_undetermined: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    data: dict[str, Any]
    attempts: list[Attempt] = field(default_factory=list)
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    round2: Round2 | None = None

    @property
    def rounds(self) -> int:
        return 2 if (self.round2 and self.round2.used) else 1

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


def build_round2_messages(previous: dict[str, Any], slices: list[Slice]) -> list[dict[str, str]]:
    """Round 2's prompt: the same rules, the previous JSON verbatim, and ONLY the new source.

    Round-1 evidence is deliberately not repeated — it is the expensive half of the request
    and the model already reported what it read there.
    """
    user = ROUND2_INSTRUCTIONS.format(
        previous=json.dumps(previous, indent=1, sort_keys=True),
        schema=SCHEMA_SKELETON,
        check_types=", ".join(ALLOWED_CHECK_TYPES),
        evidence=render_slices(slices),
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
# Round 2: follow the pointers round 1 handed us
# ---------------------------------------------------------------------------

#: Statuses that mean "the model did not settle this from what it saw".
OPEN_STATUSES = ("undetermined", "inferred")

#: `pointer` is free text in practice ("src/tools.py:88 (the docstring)"), so the first
#: whitespace-delimited token is taken and stripped of quoting and trailing punctuation.
_POINTER_TRIM = "`'\"(),;"
_POINTER_RE = re.compile(r"^(?P<path>[^\s:]+):(?P<line>\d+)(?:-\d+)?$")


def parse_pointer(pointer: str) -> tuple[str, int | None] | None:
    """`"path:line"` or `"path"` -> (path, line|None). None when there is no path at all.

    Tolerates a missing line number, a line range (the start wins), and trailing prose.
    """
    text = str(pointer or "").strip()
    if not text:
        return None
    head = text.split()[0].strip(_POINTER_TRIM)
    if not head:
        return None
    match = _POINTER_RE.match(head)
    if match:
        return match.group("path"), int(match.group("line"))
    if ":" in head:  # "path:notaline" — keep the path, drop the junk
        head = head.split(":", 1)[0]
    return (head, None) if head else None


def _claim_pointers(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(raw pointer, source label) pairs, in the order the report should show them.

    Two sources, exactly as the feature is specified: `undetermined[].pointer`, and the
    citation of any claim the model left open (`status` undetermined/inferred, or a chunk /
    tool whose `kind` is inferred).
    """
    out: list[tuple[str, str]] = []
    for index, item in enumerate(data.get("undetermined") or []):
        if not isinstance(item, dict):
            continue
        pointer = str(item.get("pointer") or "").strip()
        if pointer:
            out.append((pointer, f"undetermined[{index}] {item.get('what') or ''}".strip()))

    for key in ("endpoint", "model", "params", "max_turns"):
        claim = data.get(key)
        if isinstance(claim, dict) and claim.get("status") in OPEN_STATUSES:
            citation = str(claim.get("citation") or "").strip()
            if citation:
                out.append((citation, key))

    prompt = data.get("system_prompt") if isinstance(data.get("system_prompt"), dict) else {}
    prompt_open = prompt.get("status") in OPEN_STATUSES
    for index, chunk in enumerate(prompt.get("chunks") or []):
        if not isinstance(chunk, dict):
            continue
        if prompt_open or chunk.get("kind") == "inferred":
            citation = str(chunk.get("citation") or "").strip()
            if citation:
                out.append((citation, f"system_prompt.chunks[{index}]"))

    for index, tool in enumerate(data.get("tools") or []):
        if isinstance(tool, dict) and tool.get("kind") == "inferred":
            citation = str(tool.get("citation") or "").strip()
            if citation:
                out.append((citation, f"tools[{index}] {tool.get('name') or ''}".strip()))
    return out


def resolve_pointer_path(root: Path, path: str) -> str | None:
    """A pointer's path -> a repo-relative posix path, or None.

    None when the file does not exist, is not readable source, or resolves outside the repo:
    a pointer is model output, so `../../etc/passwd` must not become evidence.
    """
    raw = str(path or "").strip().strip(_POINTER_TRIM)
    if not raw:
        return None
    root = Path(root)
    candidate = Path(raw) if Path(raw).is_absolute() else root / raw
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    if not resolved.is_file() or not readable_source(resolved):
        return None
    try:
        return resolved.relative_to(root_resolved).as_posix()
    except ValueError:
        return None


def collect_pointers(data: dict[str, Any], root: Path) -> list[Pointer]:
    """Every open claim's pointer that resolves to a real file inside the repo.

    Whether a pointer is worth *following* is not decided here — that needs the file itself
    (see `plan_round2`), because a pointer into a file round 1 ranked can still name lines no
    slice of it contained.
    """
    pointers: list[Pointer] = []
    claimed: set[tuple[str, int | None]] = set()
    for raw, source in _claim_pointers(data):
        parsed = parse_pointer(raw)
        if parsed is None:
            continue
        rel = resolve_pointer_path(root, parsed[0])
        if rel is None:
            continue
        key = (rel, parsed[1])
        if key in claimed:
            continue
        claimed.add(key)
        pointers.append(Pointer(raw=raw, path=rel, line=parsed[1], source=source))
    return pointers


def plan_round2(inventory: Inventory, data: dict[str, Any]) -> Round2:
    """Decide whether there is a second round, and build its evidence. Makes no calls.

    A pointer earns a round 2 when it names source the model has not actually read: either a
    file with no round-1 slice at all, or — the common case in a big repo — a line range in a
    ranked file that fell in the gap between that file's round-1 windows. In the second case
    only the uncovered part of the pointed-at window is sent, so round 2 never pays to show
    the model a page it already has. A pointer whose window round 1 fully covered contributes
    nothing, and if no pointer contributes there is no second round.

    The budget is what is LEFT of the run's evidence cap after round 1, so following a
    pointer can never quietly double what an operator agreed to send.
    """
    root = Path(inventory.repo.root)
    pointers = collect_pointers(data, root)
    if not pointers:
        return Round2(skipped="no open claim pointed at a readable file in the source repo")

    covered = inventory.slice_ranges
    by_path: dict[str, list[Pointer]] = {}
    for pointer in pointers:
        by_path.setdefault(pointer.path, []).append(pointer)

    budget = max(0, inventory.max_tokens - inventory.evidence_tokens)
    slices: list[Slice] = []
    files: list[str] = []
    followed: list[Pointer] = []
    used = 0
    dropped = 0
    unread_source = False
    for rel, group in by_path.items():
        text = read_text(root / rel)
        if text is None:
            dropped += 1
            continue
        pieces = slice_pointer(
            rel,
            text,
            [p.line for p in group if p.line is not None],
            covered=covered.get(rel, ()),
        )
        if not pieces:
            continue  # round 1 already showed every line this pointer is about
        unread_source = True
        kept_any = False
        for piece in pieces:
            cost = estimate_tokens(piece.text) + 16  # the citation header line
            if used + cost > budget:
                dropped += 1
                continue
            slices.append(piece)
            used += cost
            kept_any = True
        if kept_any:
            files.append(rel)
            followed.extend(group)

    if not slices:
        return Round2(
            pointers=pointers,
            dropped_slices=dropped,
            skipped=(
                f"{len(pointers)} pointer(s) resolved, but the evidence budget had "
                f"{budget:,} token(s) left and none of the pointed-at source fit"
                if unread_source
                else f"all {len(pointers)} pointer(s) named source the round-1 evidence "
                f"already contained"
            ),
        )
    return Round2(
        pointers=pointers, followed=followed, files=files, slices=slices, evidence_tokens=used,
        dropped_slices=dropped,
    )


def _status(entry: Any) -> str:
    return entry.get("status") if isinstance(entry, dict) else ""


def settled_claims(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Claims round 2 moved out of undetermined/inferred, named for the report.

    Reported, never acted on: this is a description of a diff, not a confidence signal —
    verify.py still re-checks every citation in the round-2 JSON from scratch.
    """
    moved: list[str] = []
    for key in ("endpoint", "model", "params", "max_turns"):
        if _status(before.get(key)) in OPEN_STATUSES and _status(after.get(key)) == "found":
            moved.append(key)
    if (
        _status(before.get("system_prompt")) in OPEN_STATUSES
        and _status(after.get("system_prompt")) == "found"
    ):
        moved.append("system_prompt")

    before_tools = {
        str(t.get("name")): t.get("kind")
        for t in (before.get("tools") or [])
        if isinstance(t, dict)
    }
    for tool in after.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        if name and name not in before_tools:
            moved.append(f"tool:{name} (new)")
        elif before_tools.get(name) == "inferred" and tool.get("kind") in ("verbatim", "templated"):
            moved.append(f"tool:{name}")
    return moved


def resolved_undetermined(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """`undetermined` entries round 1 raised that round 2 no longer lists."""
    still_open = {
        str(item.get("what") or "")
        for item in (after.get("undetermined") or [])
        if isinstance(item, dict)
    }
    return [
        str(item.get("what") or "")
        for item in (before.get("undetermined") or [])
        if isinstance(item, dict) and str(item.get("what") or "") not in still_open
    ]


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def _run_round(
    messages: list[dict[str, str]],
    *,
    call_model: CallModel,
    model: str,
    params: dict[str, Any] | None,
    attempts: list[Attempt],
) -> tuple[dict[str, Any] | None, list[str]]:
    """One call plus exactly one retry on a schema violation.

    Attempts are appended to the shared list and numbered globally, so round 2's attempts are
    reps 3 and 4 of the same recorded run and `RecordingExtractor.finalize` matches them up.
    Returns (validated reply, errors); a None reply means both attempts were rejected.
    """
    last_errors: list[str] = []
    for step in (1, 2):
        request = build_request(messages, model, params)
        response = call_model(request)
        text = response_text(response)
        data, parse_error = parse_json_object(text)
        errors = [parse_error] if parse_error else validate_extraction(data or {})
        attempts.append(
            Attempt(
                index=len(attempts) + 1, request=request, response=response, text=text,
                errors=errors, ok=not errors,
            )
        )
        if not errors:
            return data or {}, []
        last_errors = errors
        if step == 2:
            break
        # Retry carries the model's own reply plus the exact violations.
        messages = [
            *messages,
            {"role": "assistant", "content": text},
            {"role": "user", "content": RETRY_PREFIX.format(
                errors="\n".join(f"- {e}" for e in errors[:20])
            )},
        ]
    return None, last_errors


def _salvage(attempts: list[Attempt]) -> dict[str, Any] | None:
    """The most recent reply that was at least JSON. Downstream treats it as low confidence."""
    for attempt in reversed(attempts):
        parsed, _ = parse_json_object(attempt.text)
        if parsed is not None:
            return parsed
    return None


def extract(
    inventory: Inventory,
    *,
    call_model: CallModel,
    model: str = "gpt-5.5",
    agent_hint: str | None = None,
    params: dict[str, Any] | None = None,
    second_round: bool = True,
) -> ExtractionResult:
    """Round 1 over the ranked evidence, then at most one round following its pointers.

    Every attempt of every round goes through `call_model`, so all of them end up in the run
    record and `upshift cost` prices the whole thing. Round 2 is best-effort by construction:
    if it is skipped, refused by the cost guard, rejected by the validator twice or fails
    outright, round 1's result stands and the report says which.
    """
    attempts: list[Attempt] = []
    raw, errors = _run_round(
        build_messages(inventory, agent_hint),
        call_model=call_model, model=model, params=params, attempts=attempts,
    )
    if raw is not None:
        result = ExtractionResult(data=normalize(raw), attempts=attempts, ok=True)
    else:
        salvaged = _salvage(attempts)
        result = ExtractionResult(
            data=normalize(salvaged) if salvaged else copy.deepcopy(EMPTY_EXTRACTION),
            attempts=attempts, ok=False, errors=errors,
        )
    if second_round:
        _second_round(inventory, result, call_model=call_model, model=model, params=params)
    return result


def _second_round(
    inventory: Inventory,
    result: ExtractionResult,
    *,
    call_model: CallModel,
    model: str,
    params: dict[str, Any] | None,
) -> None:
    """Follow round 1's pointers, in place. Never raises, never loops."""
    round2 = plan_round2(inventory, result.data)
    result.round2 = round2
    if not round2.slices:
        return

    round1_data = copy.deepcopy(result.data)
    messages = build_round2_messages(round1_data, round2.slices)
    round2.ran = True
    round2.first_attempt = len(result.attempts) + 1
    try:
        raw, errors = _run_round(
            messages, call_model=call_model, model=model, params=params,
            attempts=result.attempts,
        )
    except AdaptAborted as exc:
        # The cost guard refused the call. Round 1 already paid for itself; keep it.
        round2.ran = len(result.attempts) >= round2.first_attempt
        round2.aborted = exc.message
        return
    except Exception as exc:  # noqa: BLE001 - a bonus round must never destroy round 1
        round2.ran = len(result.attempts) >= round2.first_attempt
        round2.aborted = f"{type(exc).__name__}: {exc}"
        return

    if raw is None:
        round2.errors = errors
        return
    round2.ok = True
    round2.used = True
    result.data = normalize(raw)
    result.ok = True
    result.errors = []
    round2.settled = settled_claims(round1_data, result.data)
    round2.resolved_undetermined = resolved_undetermined(round1_data, result.data)
