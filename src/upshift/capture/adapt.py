"""`upshift adapt --from-capture` — the five adapter files, built from recorded wire bytes.

Nothing here calls a model and nothing here reads a framework's source. Every value written
comes out of a request or a response the framework actually made:

| file | where it comes from |
| --- | --- |
| `agent.json` | model, `max_tokens`, sampling params, `tool_choice`, `thinking`, effort — as sent |
| `system_prompt.txt` | the `system` blocks, concatenated verbatim |
| `tools.json` | the `tools` array, verbatim (re-wrapped chat-style, as ADAPTER.md requires) |
| `cases/cases.json` | one case per captured conversation; the user turns, verbatim |
| `backend.py` + `recorded_tools.json` | the tool results the framework fed back, replayed |

The two rules that keep this honest, and that every reviewer should check first:

* **Checks are derived, never invented.** A capture proves three things and no more: the call
  did not error, these tools were called, and it took this many turns. So the generated checks
  are exactly `no_api_error`, `tool_called` per tool the recording shows, and `turns_at_most`.
  A `response_contains` built out of the recorded answer text would be a check written from the
  model's own output — it would pass by construction on the baseline and fail on any candidate
  that phrased itself differently, which is eval overfitting, not a regression.
* **Unknown tool arguments fail loudly.** The replay backend answers from the capture keyed by
  (tool name, canonical JSON of the input). A repaired run that calls a tool with arguments the
  capture never saw gets `{"error": "no recorded result for these arguments"}` — a visible
  failure, not a silent pass.

Thinking blocks in the captured history are NOT carried into cases: upshift never replays
another model's thinking, and the differ's `thinking_block_invalid` refusal stays the answer
for that failure mode (ADAPTER.md).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from upshift.adapt.generate import RETRIEVAL_NAME_RE, build_params, docstring_safe, slugify
from upshift.capture.record import canonical, load_capture, text_blocks

ENDPOINT = "messages"
PROVIDER = "anthropic"
RECORDED_TOOLS_FILE = "recorded_tools.json"
NO_RECORD_ERROR = "no recorded result for these arguments"

#: Blocks that never become part of a case's user text (they are not text, or they are
#: another model's reasoning, which upshift does not replay).
_SKIPPED_USER_BLOCKS = ("tool_result", "tool_use", "thinking", "redacted_thinking")


@dataclass
class CaptureAdaptResult:
    out_dir: Path
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    case_count: int = 0
    model: str = ""
    framework: str = ""
    volatile_suffix: str | None = None
    terminal_tools: list[str] = field(default_factory=list)
    max_turns: int = 12
    #: The derived per-turn param sequence (agent.json `turn_params`), empty when every
    #: recorded turn of every conversation sent the same params.
    turn_params: list[dict[str, Any]] = field(default_factory=list)
    #: The subset of `notes` where the capture disagreed with ITSELF and the adapter had to
    #: choose — a value some recorded request will not reproduce. `--strict` exits non-zero
    #: on any of these, because a note is not a gate.
    conflicts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the capture
# ---------------------------------------------------------------------------


def _bodies(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every recorded request body that is a usable Messages request."""
    out = []
    for conversation in conversations:
        for turn in conversation["turns"]:
            body = (turn.get("request") or {}).get("body")
            if isinstance(body, dict) and isinstance(body.get("messages"), list):
                out.append(body)
    return out


def _ranked(counts: Counter) -> list[tuple[str, int]]:
    """Recorded values, most frequent first, TIES BROKEN BY THE CANONICAL TEXT.

    `Counter.most_common` breaks a tie by insertion order, so the same traffic recorded in a
    different order produced a different agent — and, when the tied values were a forced and
    an unforced `tool_choice`, an agent that silently did not reproduce the capture at all
    (rescue-ops `A-075` §6.3(a)). Which turn the recorder happened to start on is not
    evidence, so it never decides anything.
    """
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _variant_listing(counts: Counter) -> str:
    return ", ".join(f"{blob} ({n}x)" for blob, n in _ranked(counts))


def _most_common(
    values: list[Any], notes: list[str], what: str, conflicts: list[str] | None = None
) -> Any:
    """The most frequently sent value, recording every other one it beat.

    A capture that sent more than one value for `what` cannot be reproduced by a single
    value, so every discarded variant is both noted and recorded as a CONFLICT: the note is
    for the reader, the conflict is what `--strict` refuses on.
    """
    usable = [v for v in values if v is not None]
    if not usable:
        return None
    counts = Counter(canonical(v) for v in usable)
    winner, hits = _ranked(counts)[0]
    if len(counts) > 1:
        tied = sum(1 for count in counts.values() if count == hits) > 1
        note = (
            f"CONFLICT: {what} varied across the capture and is not a per-turn shape the "
            f"sequence can express. Kept {winner} ({hits} of {len(usable)} requests"
            + (", chosen by canonical order because the count was TIED" if tied else "")
            + f"), variants not written: {_variant_listing(counts)}"
        )
        notes.append(note)
        if conflicts is not None:
            conflicts.append(note)
    return json.loads(winner)


# ---------------------------------------------------------------------------
# Per-turn params: the shape the framework actually sent, turn by turn
# ---------------------------------------------------------------------------

#: Request fields read off the wire into agent.json. `tool_choice` heads the list because it
#: is the one that decides CONTROL FLOW: under a forced choice the model cannot answer in
#: text, so a framework that forces turn 1 and then goes `auto` has a completely different
#: episode from one that forces every turn.
CAPTURED_PARAMS = (
    "max_tokens", "temperature", "top_p", "top_k", "tool_choice", "thinking",
    "output_config", "service_tier",
)


def _conversation_bodies(conversations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Usable request bodies per conversation, in request order — the turn index."""
    out: list[list[dict[str, Any]]] = []
    for conversation in conversations:
        bodies = [
            body
            for turn in conversation["turns"]
            if isinstance(body := (turn.get("request") or {}).get("body"), dict)
            and isinstance(body.get("messages"), list)
        ]
        if bodies:
            out.append(bodies)
    return out


def _varies_by_turn(sequences: list[list[dict[str, Any]]], key: str) -> bool:
    """True when any ONE conversation sent more than one value for `key` across its turns.

    Scoped to a single conversation on purpose: that is a per-turn shape, which the sequence
    reproduces. Two conversations that each held one value but disagreed with each other are
    a conflict, not a shape, and `_most_common` reports them as one.
    """
    return any(len({canonical(body.get(key)) for body in bodies}) > 1 for bodies in sequences)


def _agreed_turn_value(
    values: list[Any], notes: list[str], conflicts: list[str], what: str
) -> Any:
    """The one value every conversation sent at this turn, or a reported choice between them.

    `None` means the field was not sent on that turn, which is a different request from any
    value it could have carried — so it is carried through as `null` rather than smoothed
    into the neighbouring turn's value.
    """
    counts = Counter(canonical(value) for value in values)
    if not counts:
        return None
    winner, hits = _ranked(counts)[0]
    if len(counts) > 1:
        note = (
            f"CONFLICT: {what} — the captured conversations disagree, and no per-turn "
            f"sequence can hold both. Variants, most frequent first: "
            f"{_variant_listing(counts)}. Wrote {winner} ({hits} of {len(values)} "
            f"conversations that reached this turn), chosen by count then by canonical order "
            f"— never by which conversation the recorder saw first. Check it against the "
            f"framework before trusting a run built on this directory."
        )
        notes.append(note)
        conflicts.append(note)
    return json.loads(winner)


def _turn_entry(raw: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """One turn's overrides, canonicalised exactly the way `params` is.

    `None` (the framework did not send the field on this turn) is carried around
    `build_params`, which is about values; on the way back out it is what tells the agent
    loop to UNSET the param for that turn.
    """
    unset = [key for key, value in raw.items() if value is None]
    sent = {key: value for key, value in raw.items() if value is not None}
    fresh: list[str] = []
    entry = build_params(sent, ENDPOINT, fresh) if sent else {}
    # The same canonicalisation note would otherwise land once per turn.
    notes.extend(note for note in fresh if note not in notes)
    for key in unset:
        entry[key] = None
    return entry


def _derive_turn_params(
    sequences: list[list[dict[str, Any]]], keys: list[str], notes: list[str],
    conflicts: list[str],
) -> list[dict[str, Any]]:
    """The `turn_params` sequence for the params that vary by turn.

    Trailing duplicates are dropped because the last entry repeats for every later turn, so
    `[force, auto, auto]` and `[force, auto]` describe the same agent and the shorter one is
    the one a human can read.
    """
    length = max(len(bodies) for bodies in sequences)
    entries: list[dict[str, Any]] = []
    for index in range(length):
        raw = {
            key: _agreed_turn_value(
                [bodies[index].get(key) for bodies in sequences if index < len(bodies)],
                notes, conflicts, f"the {key!r} parameter at turn {index + 1}",
            )
            for key in keys
        }
        entries.append(_turn_entry(raw, notes))
    while len(entries) > 1 and entries[-1] == entries[-2]:
        entries.pop()
    return entries


def _system_text(body: dict[str, Any]) -> str | None:
    system = body.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [
            str(block.get("text") or "")
            for block in system
            if isinstance(block, dict) and block.get("type") in (None, "text")
        ]
        return "".join(parts) if parts else None
    return None


def _tools_chat_style(tools: Any) -> list[dict[str, Any]]:
    """Anthropic `{name, description, input_schema}` -> the chat-style shape ADAPTER.md wants.

    `agent_loop.convert_tools_messages` converts it straight back before the request goes out,
    so the wire body is the recorded one. `cache_control` marks are dropped here because
    upshift places its own breakpoints (`agent_loop._mark_last_tool_cacheable`).
    """
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _tool_calls(response_body: Any) -> list[dict[str, Any]]:
    blocks = (response_body or {}).get("content") or []
    return [
        b for b in blocks
        if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("name"), str)
    ]


def _response_text(response_body: Any) -> str:
    blocks = (response_body or {}).get("content") or []
    return "".join(
        str(b.get("text") or "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _ok(turn: dict[str, Any]) -> dict[str, Any] | None:
    """The response body of a turn that actually succeeded, else None."""
    response = turn.get("response") or {}
    body = response.get("body")
    if response.get("status") == 200 and isinstance(body, dict) and body.get("content") is not None:
        return body
    return None


# ---------------------------------------------------------------------------
# Replay data: (tool name, canonical arguments) -> the result the framework returned
# ---------------------------------------------------------------------------


def build_replay(conversations: list[dict[str, Any]], notes: list[str]) -> dict[str, dict[str, Any]]:
    """The tool results the capture contains, keyed for `backend.py`."""
    recorded: dict[str, dict[str, Any]] = {}
    for conversation in conversations:
        pending: dict[str, tuple[str, Any]] = {}  # tool_use id -> (name, input)
        for turn in conversation["turns"]:
            for block in _tool_calls(_ok(turn) or {}):
                pending[str(block.get("id"))] = (block["name"], block.get("input") or {})
            body = (turn.get("request") or {}).get("body") or {}
            for message in body.get("messages") or []:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                for block in text_blocks(message.get("content")):
                    if block.get("type") != "tool_result":
                        continue
                    call = pending.get(str(block.get("tool_use_id")))
                    if call is None:
                        continue
                    name, arguments = call
                    result, note = _result_dict(block.get("content"), name)
                    if note:
                        notes.append(note)
                    recorded.setdefault(name, {})[canonical(arguments)] = result
    return recorded


def terminal_tool_names(conversations: list[dict[str, Any]]) -> list[str]:
    """Tools the framework never fed a result back for — so calling one ended the conversation.

    Read off the capture, not guessed: every recorded `tool_use` id is matched against the
    `tool_result` blocks of the later requests. A name that was answered even once is an
    ordinary tool; a name that was never answered anywhere in the capture is terminal, because
    the framework stopped the moment the model called it.

    This is what pydantic-ai's `final_result` is (structured `output_type`), and what every
    framework's "the model has produced the output, we are done" tool is. Without it the
    replayed episode hands the model a result the real client never produced, and the model —
    still under a forced `tool_choice` — calls it again: one extra assistant turn that the
    real agent could not have taken, which fails the case's own `turns_at_most` on the
    BASELINE model. Found live, on a pydantic-ai capture.
    """
    called: set[str] = set()
    answered: set[str] = set()
    for conversation in conversations:
        pending: dict[str, str] = {}
        for turn in conversation["turns"]:
            for block in _tool_calls(_ok(turn) or {}):
                pending[str(block.get("id"))] = str(block.get("name"))
                called.add(str(block.get("name")))
            body = (turn.get("request") or {}).get("body") or {}
            for message in body.get("messages") or []:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                for block in text_blocks(message.get("content")):
                    if block.get("type") != "tool_result":
                        continue
                    name = pending.get(str(block.get("tool_use_id")))
                    if name is not None:
                        answered.add(name)
    return sorted(called - answered)


def _result_dict(content: Any, name: str) -> tuple[dict[str, Any], str | None]:
    """A recorded tool_result payload as the dict `Backend.execute` must return.

    ADAPTER.md requires a dict; a tool that returned a bare string or a list is wrapped under
    `content`, and that wrapping is the note this returns, because it changes the bytes the
    model sees by exactly one JSON envelope.
    """
    payload: Any = content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        payload = "".join(parts)
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError:
            return {"content": payload}, (
                f"{name}: a recorded tool result was plain text, wrapped as "
                f'{{"content": ...}} because ADAPTER.md requires execute() to return a dict'
            )
        payload = decoded
    if isinstance(payload, dict):
        return payload, None
    return {"content": payload}, (
        f"{name}: a recorded tool result was a {type(payload).__name__}, wrapped as "
        f'{{"content": ...}} because ADAPTER.md requires execute() to return a dict'
    )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _strip_volatile(message: dict[str, Any], volatile: dict[str, Any] | None) -> dict[str, Any]:
    """The user message without its per-request volatile part."""
    if not volatile:
        return message
    blocks = text_blocks(message.get("content"))
    if volatile.get("kind") == "block" and len(blocks) >= 2:
        return {"role": "user", "content": blocks[:-1]}
    return {"role": "user", "content": []}


def _user_segments(
    messages: list[Any], volatile_index: int | None, volatile: dict[str, Any] | None,
    notes: list[str], case_id: str,
) -> list[str]:
    """The user turns of a conversation, verbatim, in order."""
    segments: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if index == volatile_index:
            message = _strip_volatile(message, volatile)
        blocks = text_blocks(message.get("content"))
        if any(b.get("type") == "tool_result" for b in blocks):
            continue  # a tool-result turn, not something a user typed
        dropped = {
            str(b.get("type")) for b in blocks
            if b.get("type") not in (None, "text") and b.get("type") not in _SKIPPED_USER_BLOCKS
        }
        if dropped:
            notes.append(
                f"{case_id}: dropped non-text content block(s) {sorted(dropped)} from a user "
                f"turn — an upshift case input is text (ADAPTER.md)"
            )
        text = "\n".join(
            str(b.get("text") or "") for b in blocks if b.get("type") in (None, "text")
        )
        if text.strip():
            segments.append(text)
    return segments


def build_case(
    conversation: dict[str, Any], index: int, notes: list[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """One eval case from one captured conversation. -> (case, tool names it called)."""
    turns = conversation["turns"]
    last_request = (turns[-1].get("request") or {}).get("body") or {}
    messages = last_request.get("messages")
    if not isinstance(messages, list) or not messages:
        notes.append(f"{conversation['id']}: no messages array recorded, conversation skipped")
        return None, []
    volatile = conversation.get("volatile_suffix")
    volatile_index = volatile.get("index") if isinstance(volatile, dict) else None

    provisional_id = f"{conversation['id']}"
    segments = _user_segments(messages, volatile_index, volatile, notes, provisional_id)
    if not segments:
        notes.append(
            f"{conversation['id']}: no user turn carried text (image-only or tool-only "
            f"conversation), skipped"
        )
        return None, []

    case_id = _case_id(segments[0], index)
    plan: list[dict[str, Any]] = []
    called: list[str] = []
    answered = 0
    errors: list[str] = []
    for turn in turns:
        body = _ok(turn)
        if body is None:
            status = (turn.get("response") or {}).get("status")
            errors.append(str(status))
            continue
        answered += 1
        calls = _tool_calls(body)
        if calls:
            plan.append(
                {
                    "tool_calls": [
                        {"name": c["name"], "arguments": c.get("input") or {}} for c in calls
                    ]
                }
            )
            for call in calls:
                if call["name"] not in called:
                    called.append(call["name"])
            if _response_text(body).strip():
                notes.append(
                    f"{case_id}: an assistant turn returned text alongside its tool call(s); "
                    f"the sim oracle plan keeps the calls and drops the text (the plan is one "
                    f"kind of step per turn)"
                )
        else:
            plan.append({"final_message": _response_text(body)})
    if errors:
        notes.append(
            f"{case_id}: the capture itself recorded {len(errors)} failed response(s) "
            f"(status {', '.join(errors)}) — the `no_api_error` check will fail on the "
            f"baseline too, which is what the recording says happened"
        )

    checks: list[dict[str, Any]] = [{"type": "no_api_error"}]
    for name in called:
        check: dict[str, Any] = {"type": "tool_called", "name": name, "min_times": 1}
        if RETRIEVAL_NAME_RE.search(name):
            # Inert for pass/fail; read only by the differ's reduced_retrieval_calls signature.
            check["retrieval"] = True
        checks.append(check)
    checks.append({"type": "turns_at_most", "n": answered + 1})

    agents = (turns[0].get("request") or {}).get("headers", {}).get("user-agent", "")
    case = {
        "id": case_id,
        "description": (
            f"Captured conversation {conversation['id']}: {len(segments)} user turn(s), "
            f"{answered} recorded assistant turn(s), "
            f"{len(called)} tool(s) called"
            + (f"; client {agents}" if agents else "")
        ),
        "initial_state": {},
        "user_messages": segments,
        "checks": checks,
        "sim": {"oracle_plan": plan},
    }
    return case, called


def _case_id(first_segment: str, index: int) -> str:
    """A stable, filesystem-safe case id: it names a directory under the runs root."""
    slug = slugify(" ".join(first_segment.split()[:6]), fallback="conversation")[:40]
    slug = slug.strip("-_") or "conversation"
    if not slug[0].isalnum():
        slug = f"c{slug}"
    return f"{slug}_{index:02d}"


# ---------------------------------------------------------------------------
# backend.py
# ---------------------------------------------------------------------------

BACKEND_TEMPLATE = '''"""Record-and-replay tool backend, generated by `upshift adapt --from-capture`.

Source capture: __ORIGIN__
Generated: __GENERATED__

Every result this returns was returned by the real tool, to the real framework, during the
capture. Nothing is re-implemented and nothing is invented: results are looked up by tool name
and by the canonical JSON of the arguments, exactly as recorded.

A call whose arguments were never recorded returns
`{"error": "__NO_RECORD__"}`. That is deliberate. A repaired candidate that calls a tool with
new arguments has left the ground the capture covers, and a replay that quietly answered
anyway would turn "we do not know" into a passing case. It fails, visibly, and `state()`
lists every such call so the report shows exactly which ones they were.

Deterministic by construction (a pure lookup in a JSON file), as ADAPTER.md requires.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

NO_RECORD = "__NO_RECORD__"

_RECORDED: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parent / "__RECORDED_FILE__").read_text(encoding="utf-8")
)


def canonical(arguments: Any) -> str:
    """The key the capture was written with (upshift.capture.record.canonical)."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


class Backend:
    """Replays recorded tool results. Never raises, as ADAPTER.md requires."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        # `initial_state` is unused by design: a capture has no world to seed, only answers.
        self._calls: list[dict[str, Any]] = []
        self._unrecorded: list[dict[str, Any]] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._calls.append({"name": name, "arguments": arguments})
        by_arguments = _RECORDED.get(name)
        if by_arguments is None:
            return self._miss(name, arguments, f"the capture never recorded a call to {name!r}")
        key = canonical(arguments)
        if key in by_arguments:
            return copy.deepcopy(by_arguments[key])
        return self._miss(
            name, arguments,
            f"{name} was called in the capture, but never with these arguments",
        )

    def state(self) -> dict[str, Any]:
        return {"tool_calls": self._calls, "unrecorded_calls": self._unrecorded}

    def _miss(self, name: str, arguments: dict[str, Any], detail: str) -> dict[str, Any]:
        self._unrecorded.append({"name": name, "arguments": arguments})
        recorded = sorted(_RECORDED.get(name) or {})
        return {
            "error": NO_RECORD,
            "detail": detail,
            "tool": name,
            "recorded_argument_sets": recorded[:5],
        }


def create_backend(initial_state: dict[str, Any]) -> Backend:
    return Backend(initial_state)
'''


def build_backend(origin: str) -> str:
    return (
        BACKEND_TEMPLATE.replace("__ORIGIN__", docstring_safe(origin))
        .replace("__GENERATED__", datetime.now(UTC).strftime("%Y-%m-%d"))
        .replace("__RECORDED_FILE__", RECORDED_TOOLS_FILE)
        .replace("__NO_RECORD__", NO_RECORD_ERROR)
    )


# ---------------------------------------------------------------------------
# The ledgers
# ---------------------------------------------------------------------------


def _attribution(index: dict[str, Any], capture_dir: Path, conversations: list[dict[str, Any]],
                 model: str) -> str:
    framework = index.get("framework") or {}
    lines = [
        "# ATTRIBUTION — an agent directory built from captured wire traffic",
        "",
        "## Where this came from",
        "",
        f"- Capture directory: `{capture_dir}`",
        f"- Recorded: {index.get('created_at')} → {index.get('closed_at')}",
        f"- Mode: `{index.get('mode')}`"
        + (f", upstream `{index.get('upstream')}`" if index.get("upstream") else ""),
        f"- Requests: {index.get('requests')} in {index.get('conversations')} conversation(s)",
        f"- Model(s) seen: {', '.join(index.get('models') or []) or 'none'}",
        f"- Framework: {framework.get('framework') or 'unknown'}"
        + (f" (override: `--framework {framework['override']}`)" if framework.get("override")
           else ""),
        "",
    ]
    if framework.get("evidence"):
        lines += ["Detection evidence, verbatim from the request headers:", ""]
        lines += [f"- `{name}` — {how}" for name, how in sorted(framework["evidence"].items())]
        lines.append("")
    if framework.get("user_agents"):
        lines += ["User agents observed:", ""]
        lines += [f"- `{agent}`" for agent in framework["user_agents"]]
        lines.append("")
    lines += [
        "## What was taken, and from where",
        "",
        "| Artifact | Source |",
        "| --- | --- |",
        "| `system_prompt.txt` | the `system` field of the recorded requests, verbatim |",
        "| `tools.json` | the `tools` array of the recorded requests, verbatim |",
        f"| `agent.json` `model` | `{model}`, as sent |",
        ("| `agent.json` `params` | `max_tokens` / `tool_choice` / `thinking` / sampling params "
        "/ effort, as sent |"),
        "| `cases/cases.json` | one case per captured conversation; user turns verbatim |",
        "| `recorded_tools.json` | the `tool_result` payloads the framework fed back |",
        "",
        "## Conversations",
        "",
        "| id | requests | volatile suffix |",
        "| --- | ---: | --- |",
    ]
    for conversation in conversations:
        volatile = conversation.get("volatile_suffix")
        mark = f"{volatile['kind']} @ message {volatile['index']}" if volatile else "none"
        lines.append(f"| `{conversation['id']}` | {len(conversation['turns'])} | {mark} |")
    lines += [
        "",
        ("No source file of the framework was read to produce any of this. upshift saw the "
        "bytes on the wire and nothing else — which is the whole point of capture mode: it "
        "works the same for a framework whose request path nobody can lift into five files."),
        "",
    ]
    return "\n".join(lines)


def _adapt_edits(result: CaptureAdaptResult, index: dict[str, Any]) -> str:
    lines = [
        "# ADAPT_EDITS — every deviation from the recorded bytes",
        "",
        ("`upshift adapt --from-capture` copies the wire. Where it could not copy exactly, the "
        "deviation is listed here. Nothing else was changed."),
        "",
        "## Structural deviations (always true of a capture-derived adapter)",
        "",
        ("1. **Thinking blocks are not replayed.** `thinking` / `redacted_thinking` blocks in "
        "the captured history are dropped from the cases. A thinking block's signature is "
        "valid only against the exact turn that produced it, so replaying one from another "
        "model's run is what produces the ``Invalid `signature` in `thinking` block`` 400 in "
        "the first place. The differ keeps its detect-and-refuse for that signature "
        "(ADAPTER.md); capture mode does not try to route around it."),
        ("2. **Tool schemas are re-wrapped.** Recorded `{name, description, input_schema}` is "
        "written chat-style, because ADAPTER.md requires that shape on every endpoint; "
        "`agent_loop.convert_tools_messages` converts it back before the request goes out, so "
        "the wire body is the recorded one."),
        ("3. **`cache_control` marks are dropped** from the system and tool definitions. "
        "upshift places its own cache breakpoints (`agent_loop._mark_last_tool_cacheable`), "
        "so keeping the framework's would double them."),
        ("4. **Checks are derived, not invented.** Only `no_api_error`, `tool_called` for tools "
        "the recording shows being called, and `turns_at_most` (recorded assistant turns + 1). "
        "No check is built out of the recorded answer text: that would be an assertion written "
        "from the model's own output."),
        ("5. **`sim.oracle_plan` is the recorded behaviour**, replayed by `--provider sim` only. "
        "It is never read by a check (ADAPTER.md), and a sim run is machinery validation, "
        "never evidence."),
        ("6. **The backend is a replay, not a re-implementation.** Unknown arguments return "
        f"`{{\"error\": \"{NO_RECORD_ERROR}\"}}` rather than a plausible answer."),
        ("7. **A param that varies by turn is a sequence, not an average.** When one "
        "conversation sent different values for the same param on different turns, they are "
        "written as `turn_params` (below) and NOT as one episode-level value. Collapsing "
        "them is what turns a framework that forces a tool on turn 1 and then goes `auto` "
        "into one that forces every turn — an agent that can never answer in text, "
        "stable-fails its own `turns_at_most` on the BASELINE model, and gets a `SAFE` "
        "verdict over a suite that never worked (rescue-ops `A-075` §6.3(a))."),
        "",
        "## Deviations specific to this capture",
        "",
    ]
    if result.notes:
        lines += [f"- {note}" for note in result.notes]
    else:
        lines.append("- none: every request in the capture had the same shape.")
    if result.turn_params:
        lines += [
            "",
            "## Per-turn parameters (`agent.json` `turn_params`)",
            "",
            ("These params were not constant across the turns of a conversation, so they are "
            "replayed by TURN INDEX rather than collapsed to one value. The last entry "
            "repeats for every later turn, and `null` means the framework did not send that "
            "field on that turn at all (which is a different request from sending a default)."),
            "",
            "```json",
            json.dumps(result.turn_params, indent=2),
            "```",
            "",
        ]
    if result.conflicts:
        lines += [
            "",
            "## \u26a0 CONFLICTS — the capture disagrees with itself",
            "",
            (f"{len(result.conflicts)} value(s) below could not be reproduced from the "
            "recording: two or more requests sent different things and no single value (and "
            "no per-turn sequence) can hold both. Every variant is listed; the one written "
            "was chosen by count and then by canonical text, never by which request the "
            "recorder happened to see first. **`upshift adapt --from-capture --strict` exits "
            "non-zero on this section**, because a run built on a directory that does not "
            "reproduce its own capture measures an agent nobody has."),
            "",
        ]
        lines += [f"- {conflict}" for conflict in result.conflicts]
        lines.append("")
    lines += [
        "",
        "## Not deviations, but read before trusting a run",
        "",
        (f"- `max_turns` is {result.max_turns} — the longest recorded conversation plus one, so "
        "a repaired candidate has room for one extra turn without being cut off."),
        ("- Every case starts from an empty `initial_state`: a capture records answers, not a "
        "world to seed."),
        f"- Params seen across the capture: `{json.dumps(index.get('params_seen') or {})}`.",
        "",
    ]
    if result.volatile_suffix is not None:
        lines += [
            "## Volatile per-request suffix",
            "",
            ("The framework regenerated the trailing user block on every request in a "
            "conversation (the shape recorded in rescue-ops `cases/A-015/REPORT.md` §4). One "
            "recorded sample is written to `agent.json` as `volatile_suffix` and the agent "
            "loop appends it to every request; upshift never generates new values for it, so "
            "what the model sees is a real block that a real request carried, frozen."),
            "",
            "```",
            result.volatile_suffix[:2000],
            "```",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def adapt_from_capture(capture_dir: str | Path, out_dir: str | Path) -> CaptureAdaptResult:
    """Write the agent directory. Raises ValueError with a one-line reason on bad input."""
    capture_dir = Path(capture_dir)
    out_dir = Path(out_dir)
    index, conversations = load_capture(capture_dir)
    result = CaptureAdaptResult(out_dir=out_dir)
    notes = result.notes
    conflicts = result.conflicts

    bodies = _bodies(conversations)
    if not bodies:
        raise ValueError(
            f"{capture_dir} recorded no usable /v1/messages request body (nothing to adapt)"
        )

    # -- system_prompt.txt --------------------------------------------------
    prompt = (
        _most_common([_system_text(b) for b in bodies], notes, "the system prompt", conflicts)
        or ""
    )
    block_counts = {
        len(b["system"]) for b in bodies if isinstance(b.get("system"), list)
    }
    if block_counts and max(block_counts) > 1:
        notes.append(
            f"the system prompt arrived as {max(block_counts)} blocks; system_prompt.txt is "
            f"their verbatim concatenation, and upshift sends it back as one block"
        )

    # -- tools.json ---------------------------------------------------------
    by_name: dict[str, list[Any]] = {}
    for body in bodies:
        for tool in body.get("tools") or []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                by_name.setdefault(tool["name"], []).append(
                    {k: v for k, v in tool.items() if k != "cache_control"}
                )
    tools = [
        _most_common(variants, notes, f"the definition of tool {name!r}", conflicts)
        for name, variants in by_name.items()
    ]
    chat_tools = _tools_chat_style(tools)
    result.tool_names = [t["function"]["name"] for t in chat_tools]

    # -- params, and the per-turn sequence ----------------------------------
    model = _most_common([b.get("model") for b in bodies], notes, "the model", conflicts) or ""
    sequences = _conversation_bodies(conversations)
    by_turn = [key for key in CAPTURED_PARAMS if _varies_by_turn(sequences, key)]
    raw_params: dict[str, Any] = {}
    for key in CAPTURED_PARAMS:
        if key in by_turn:
            continue  # its value belongs to the turn it was sent on, not to the episode
        value = _most_common([b.get(key) for b in bodies], notes, f"the {key!r} parameter",
                             conflicts)
        if value is not None:
            raw_params[key] = value
    params = build_params(raw_params, ENDPOINT, notes)
    if by_turn:
        result.turn_params = _derive_turn_params(sequences, by_turn, notes, conflicts)
        notes.append(
            f"param(s) {by_turn} were sent with different values on different turns of one "
            f"conversation, so they are written as agent.json `turn_params` (applied by turn "
            f"index, last entry repeating) instead of one episode-level value: "
            f"{json.dumps(result.turn_params)}"
        )

    # -- cases --------------------------------------------------------------
    cases: list[dict[str, Any]] = []
    called_tools: list[str] = []
    for position, conversation in enumerate(conversations, start=1):
        case, called = build_case(conversation, position, notes)
        if case is None:
            continue
        cases.append(case)
        called_tools += [name for name in called if name not in called_tools]
    if not cases:
        raise ValueError(
            f"{capture_dir}: no captured conversation produced a case (every one was skipped; "
            f"see the reasons printed above)"
        )
    missing = [name for name in called_tools if name not in result.tool_names]
    if missing:
        notes.append(
            f"tool(s) {sorted(missing)} were called in the capture but never declared in a "
            f"recorded `tools` array; a `tool_called` check names them anyway, because the "
            f"recording says they were called"
        )
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        for position, case in enumerate(cases, start=1):
            case["id"] = f"{case['id']}_{position}"
        notes.append("two conversations produced the same case id; suffixed them to keep ids unique")

    # -- volatile suffix ----------------------------------------------------
    volatile_samples = [
        conversation["volatile_suffix"]
        for conversation in conversations
        if isinstance(conversation.get("volatile_suffix"), dict)
    ]
    if volatile_samples:
        result.volatile_suffix = str(volatile_samples[-1].get("text") or "")
        notes.append(
            f"detected a per-request volatile suffix in {len(volatile_samples)} of "
            f"{len(conversations)} conversation(s); the last recorded sample is written as "
            f"agent.json `volatile_suffix` and appended to every request"
        )

    # -- terminal tools -----------------------------------------------------
    result.terminal_tools = terminal_tool_names(conversations)
    if result.terminal_tools:
        notes.append(
            f"tool(s) {result.terminal_tools} were called but never answered with a "
            f"tool_result anywhere in the capture, so the framework ended the conversation on "
            f"them; written as agent.json `terminal_tools` and the episode stops there too"
        )

    # -- max_turns ----------------------------------------------------------
    result.max_turns = max(len(c["turns"]) for c in conversations) + 1

    # -- write --------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)
    framework = (index.get("framework") or {}).get("framework") or ""
    config: dict[str, Any] = {
        "name": slugify(f"{framework or 'capture'}-{capture_dir.name}", fallback="captured-agent"),
        "provider": PROVIDER,
        "endpoint": ENDPOINT,
        "model": model,
        "params": params,
        **({"turn_params": result.turn_params} if result.turn_params else {}),
        "system_prompt_file": "system_prompt.txt",
        "tools_file": "tools.json",
        "max_turns": result.max_turns,
    }
    if result.volatile_suffix is not None:
        config["volatile_suffix"] = result.volatile_suffix
    if result.terminal_tools:
        config["terminal_tools"] = result.terminal_tools
    config["capture"] = {
        "directory": str(capture_dir),
        "framework": framework,
        "conversations": len(conversations),
        "requests": index.get("requests"),
        "recorded_at": index.get("created_at"),
        "mode": index.get("mode"),
    }

    replay = build_replay(conversations, notes)
    result.model = model
    result.framework = framework
    result.case_count = len(cases)

    _write(out_dir, "agent.json", json.dumps(config, indent=2) + "\n", result)
    _write(out_dir, "system_prompt.txt", prompt, result)
    _write(out_dir, "tools.json", json.dumps(chat_tools, indent=2) + "\n", result)
    _write(out_dir, RECORDED_TOOLS_FILE, json.dumps(replay, indent=1, sort_keys=True) + "\n",
           result)
    _write(out_dir, "backend.py", build_backend(str(capture_dir)), result)
    _write(out_dir, "cases/cases.json", json.dumps(cases, indent=2) + "\n", result)
    _write(out_dir, "ATTRIBUTION.md",
           _attribution(index, capture_dir, conversations, model), result)
    _write(out_dir, "ADAPT_EDITS.md", _adapt_edits(result, index), result)
    return result


def _write(out_dir: Path, rel: str, content: str, result: CaptureAdaptResult) -> None:
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.files.append(rel)
