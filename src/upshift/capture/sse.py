"""Server-sent-events parsing and Messages-API reassembly.

A streaming `/v1/messages` response is a sequence of SSE events; the thing upshift needs to
record is the message those events add up to, in exactly the shape a non-streaming call would
have returned — that is what `agent_loop.parse_response` reads and what the adapter is built
from. The raw event list is recorded alongside it, verbatim, so nothing is lost.

Reassembly follows the documented event sequence: `message_start` carries the message shell,
`content_block_start` / `content_block_delta` / `content_block_stop` build `content` block by
block, and `message_delta` carries the final `stop_reason` / `stop_sequence` plus the output
token count. Deltas this module understands: `text_delta`, `input_json_delta`,
`thinking_delta`, `signature_delta`. An unknown delta type is kept in the event list and
skipped during reassembly rather than guessed at.
"""

from __future__ import annotations

import json
from typing import Any

#: An SSE frame is terminated by a blank line; fields are `name: value`.
_DATA = "data:"
_EVENT = "event:"


def parse_events(text: str) -> list[dict[str, Any]]:
    """Raw SSE text -> [{"event": <name>, "data": <parsed JSON or {"_raw": str}>}].

    Tolerant on purpose: this runs over bytes a third party produced, and a frame that does
    not parse is recorded as raw text instead of aborting the capture.
    """
    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith(_EVENT):
                name = line[len(_EVENT) :].strip()
            elif line.startswith(_DATA):
                data_lines.append(line[len(_DATA) :].lstrip())
        if name is None and not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            data: Any = json.loads(payload) if payload else None
        except ValueError:
            data = {"_raw": payload}
        if name is None and isinstance(data, dict):
            name = data.get("type")
        events.append({"event": name, "data": data})
    return events


def reassemble(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The final message dict a streamed response adds up to, or None if it never started.

    The result is shaped exactly like a non-streaming Messages response, so a capture of a
    streaming framework and a capture of a non-streaming one produce the same adapter.
    """
    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    partial_json: dict[int, list[str]] = {}

    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        kind = event.get("event") or data.get("type")
        if kind == "message_start":
            start = data.get("message")
            if isinstance(start, dict):
                message = json.loads(json.dumps(start))  # deep copy, JSON-safe by construction
                message.setdefault("content", [])
        elif kind == "content_block_start":
            index = _index(data)
            block = data.get("content_block")
            if index is not None and isinstance(block, dict):
                blocks[index] = json.loads(json.dumps(block))
                partial_json[index] = []
        elif kind == "content_block_delta":
            index = _index(data)
            delta = data.get("delta")
            if index is not None and isinstance(delta, dict):
                _apply_delta(blocks.setdefault(index, {}), partial_json.setdefault(index, []), delta)
        elif kind == "content_block_stop":
            index = _index(data)
            if index is not None:
                _finish_block(blocks.get(index), partial_json.get(index))
        elif kind == "message_delta" and message is not None:
            delta = data.get("delta")
            if isinstance(delta, dict):
                message.update({k: v for k, v in delta.items() if k != "type"})
            usage = data.get("usage")
            if isinstance(usage, dict):
                merged = dict(message.get("usage") or {})
                merged.update(usage)
                message["usage"] = merged

    if message is None:
        return None
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


def _index(data: dict[str, Any]) -> int | None:
    value = data.get("index")
    return value if isinstance(value, int) else None


def _apply_delta(block: dict[str, Any], partial: list[str], delta: dict[str, Any]) -> None:
    kind = delta.get("type")
    if kind == "text_delta" and isinstance(delta.get("text"), str):
        block["text"] = str(block.get("text") or "") + delta["text"]
    elif kind == "thinking_delta" and isinstance(delta.get("thinking"), str):
        block["thinking"] = str(block.get("thinking") or "") + delta["thinking"]
    elif kind == "signature_delta" and isinstance(delta.get("signature"), str):
        block["signature"] = str(block.get("signature") or "") + delta["signature"]
    elif kind == "input_json_delta" and isinstance(delta.get("partial_json"), str):
        partial.append(delta["partial_json"])
    # any other delta type: left in the recorded event list, never guessed at here


def _finish_block(block: dict[str, Any] | None, partial: list[str] | None) -> None:
    """Decode a tool_use block's accumulated `input` JSON once the block closes.

    A tool call arrives as a stream of JSON fragments; if they do not parse (a truncated
    stream), the fragments are kept under `_partial_json` so the capture still shows what
    arrived rather than silently recording `input: {}`.
    """
    if block is None or not partial:
        return
    text = "".join(partial)
    if not text.strip():
        block.setdefault("input", {})
        return
    try:
        block["input"] = json.loads(text)
    except ValueError:
        block["_partial_json"] = text
