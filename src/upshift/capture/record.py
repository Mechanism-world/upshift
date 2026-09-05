"""What a capture writes to disk, and how requests are grouped into conversations.

Layout (a contract with `capture/adapt.py`, the way `recorder.py` is a contract with
`differ.py`)::

    <out>/
      index.json                       written on shutdown
      conversations/conv_01/req_01.json
      conversations/conv_01/res_01.json

Three rules this module exists to enforce:

* **No key material is ever written.** Request headers are recorded so the framework can be
  identified and the wire shape audited, but every credential-shaped header is replaced with
  `REDACTED` before anything reaches disk. The check is a denylist of exact names plus a
  substring rule, and it runs on the way in, not on the way out.
* **Bodies are recorded verbatim, or not at all.** A body over the size cap is recorded as a
  marker with its byte count. A half-written body would be worse than a missing one, because
  everything downstream treats a recorded request as the truth about the wire.
* **Conversations are grouped by the messages array, not by connection.** A framework may use
  one HTTP connection for many conversations or many connections for one. A request whose
  `messages` extend a previous request's `messages` continues that conversation; anything else
  starts a new one. The one tolerated difference is a trailing user message (or a trailing
  text block inside it) that the framework regenerates per request — everruns' live
  dynamic-facts block, rescue-ops `cases/A-015/REPORT.md` §4 — which is recorded as the
  conversation's `volatile_suffix` instead of splitting it into n conversations of one.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from upshift import __version__
from upshift.capture import detect

REDACTED = "REDACTED"

#: Headers replaced with REDACTED before anything is written. The substring rule catches the
#: next credential header some framework invents; the exact names are the ones in use today.
SECRET_HEADERS = frozenset(
    {"x-api-key", "authorization", "proxy-authorization", "cookie", "set-cookie",
     "x-auth-token", "api-key", "openai-api-key"}
)
SECRET_SUBSTRINGS = ("api-key", "apikey", "api_key", "token", "secret", "password",
                     "credential", "authorization")

#: Headers kept verbatim. Anything not listed and not secret is dropped: a capture is evidence
#: about the request the framework built, not a packet dump of the user's machine.
KEPT_HEADERS = frozenset(
    {"user-agent", "content-type", "accept", "anthropic-version", "anthropic-beta",
     "anthropic-workspace-id", "x-app", "x-request-id"}
)

#: 10 MiB. Larger than any plausible Messages body, small enough that a runaway client cannot
#: fill the disk before the operator notices.
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024

_CONVERSATION_RE = re.compile(r"conv_(\d+)\Z")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def is_secret_header(name: str) -> bool:
    lowered = str(name).lower()
    return lowered in SECRET_HEADERS or any(part in lowered for part in SECRET_SUBSTRINGS)


def redact_headers(raw: Any) -> dict[str, str]:
    """Recordable header map: kept headers verbatim, credential headers as REDACTED.

    A credential header is recorded *as present* (`{"x-api-key": "REDACTED"}`) because whether
    the framework authenticated at all is part of the wire shape; its value never is.
    """
    out: dict[str, str] = {}
    for key, value in _header_items(raw):
        lowered = str(key).lower()
        if is_secret_header(lowered):
            out[lowered] = REDACTED
        elif lowered in KEPT_HEADERS or lowered.startswith("x-stainless-"):
            out[lowered] = str(value)
    return out


def _header_items(raw: Any):
    if raw is None:
        return []
    if hasattr(raw, "items"):
        return list(raw.items())
    return list(raw)


# ---------------------------------------------------------------------------
# Conversation grouping
# ---------------------------------------------------------------------------


def canonical(value: Any) -> str:
    """Stable JSON for equality comparisons (and for keying replayed tool results)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _messages(body: Any) -> list[Any]:
    messages = body.get("messages") if isinstance(body, dict) else None
    return messages if isinstance(messages, list) else []


def _is_plain_user(message: Any) -> bool:
    """A user message carrying no tool_result blocks — the only kind that may be volatile.

    Tool results legitimately differ between requests, but they arrive as *appended*
    messages, so they are already handled by the strict prefix rule. Excluding them here
    stops a mismatched tool-result turn from being mislabelled as a volatile suffix.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return all(
        not (isinstance(block, dict) and block.get("type") in ("tool_result", "tool_use"))
        for block in content
    )


def extends(previous: list[Any], current: list[Any]) -> tuple[bool, int | None]:
    """Does `current` continue the conversation `previous` ends?

    -> (extends?, index of the volatile message, or None when the match was exact).

    Exact: `previous` is a strict prefix of `current`. Tolerated: everything before
    `previous`'s last message matches and both sides have a plain user message in that last
    position — the per-request volatile suffix.
    """
    if len(current) <= len(previous) or not previous:
        return False, None
    if [canonical(m) for m in current[: len(previous)]] == [canonical(m) for m in previous]:
        return True, None
    cut = len(previous) - 1
    head_matches = [canonical(m) for m in current[:cut]] == [canonical(m) for m in previous[:cut]]
    if head_matches and _is_plain_user(previous[cut]) and _is_plain_user(current[cut]):
        return True, cut
    return False, None


def text_blocks(content: Any) -> list[dict[str, Any]]:
    """A message's content as a block list (a bare string becomes one text block)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def volatile_part(previous: Any, current: Any) -> tuple[str, str | None]:
    """How two differing user messages differ -> (kind, the volatile text of `current`).

    `kind` is `"block"` when every block but the last is identical and the last is text on
    both sides (the block-level shape), `"message"` when the whole message is regenerated,
    and `"unknown"` — with no text — when neither reading is safe, in which case nothing is
    recorded as volatile.
    """
    previous_blocks, current_blocks = text_blocks(previous.get("content") if isinstance(
        previous, dict) else None), text_blocks(current.get("content") if isinstance(
        current, dict) else None)
    if (
        len(previous_blocks) == len(current_blocks) >= 2
        and [canonical(b) for b in previous_blocks[:-1]]
        == [canonical(b) for b in current_blocks[:-1]]
        and previous_blocks[-1].get("type") == current_blocks[-1].get("type") == "text"
    ):
        return "block", str(current_blocks[-1].get("text") or "")
    if all(b.get("type") == "text" for b in current_blocks) and current_blocks:
        return "message", "\n".join(str(b.get("text") or "") for b in current_blocks)
    return "unknown", None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class CaptureStore:
    """Writes one capture directory. Thread-safe: the server is a threading HTTP server."""

    def __init__(
        self,
        out_dir: str | Path,
        *,
        listen: str,
        upstream: str,
        mode: str,
        framework: str | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.listen = listen
        self.upstream = upstream
        self.mode = mode
        self.framework = framework
        self.max_body_bytes = max_body_bytes
        self.created_at = datetime.now(UTC).isoformat()
        self._lock = threading.Lock()
        #: [{"id", "requests": [messages], "volatile": [(index, kind, text)]}]
        self._conversations: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        (self.out_dir / "conversations").mkdir(parents=True, exist_ok=True)

    # -- writing -----------------------------------------------------------

    @property
    def request_count(self) -> int:
        return len(self._records)

    @property
    def conversation_count(self) -> int:
        return len(self._conversations)

    def add(
        self,
        *,
        headers: Any,
        body: Any,
        raw_body_bytes: int,
        path: str,
        status: int | None,
        response_body: Any,
        events: list[dict[str, Any]] | None,
        streamed: bool,
        latency_s: float,
        error: str | None = None,
    ) -> tuple[str, int]:
        """Record one request/response pair. -> (conversation id, 1-based index)."""
        recorded_headers = redact_headers(headers)
        stored_body, oversized = self._bounded(body, raw_body_bytes)
        with self._lock:
            conversation, index, volatile = self._place(stored_body, oversized)
            request_record = {
                "conversation": conversation["id"],
                "index": index,
                "captured_at": datetime.now(UTC).isoformat(),
                "path": path,
                "headers": recorded_headers,
                "detection": detect.detection_fields(recorded_headers),
                "body": stored_body,
                "body_bytes": raw_body_bytes,
                "stream_requested": bool(isinstance(body, dict) and body.get("stream")),
                "volatile_message_index": volatile,
            }
            response_record = {
                "conversation": conversation["id"],
                "index": index,
                "status": status,
                "streamed": streamed,
                "latency_s": round(latency_s, 4),
                "body": response_body,
                "events": events if streamed else None,
                "error": error,
                "mode": self.mode,
            }
            directory = self.out_dir / "conversations" / conversation["id"]
            directory.mkdir(parents=True, exist_ok=True)
            _write_json(directory / f"req_{index:02d}.json", request_record)
            _write_json(directory / f"res_{index:02d}.json", response_record)
            self._records.append(request_record)
            return conversation["id"], index

    def _bounded(self, body: Any, raw_body_bytes: int) -> tuple[Any, bool]:
        if raw_body_bytes > self.max_body_bytes:
            return {
                "_oversized": True,
                "bytes": raw_body_bytes,
                "limit_bytes": self.max_body_bytes,
                "note": "body exceeded --max-body-bytes and was not recorded",
            }, True
        return body, False

    def _place(self, body: Any, oversized: bool) -> tuple[dict[str, Any], int, int | None]:
        """Find (or open) the conversation this request belongs to. Caller holds the lock."""
        messages = _messages(body)
        if messages and not oversized:
            for conversation in reversed(self._conversations):
                previous = conversation["requests"][-1]
                ok, volatile_index = extends(previous, messages)
                if not ok:
                    continue
                if volatile_index is not None:
                    kind, text = volatile_part(previous[volatile_index], messages[volatile_index])
                    if text is None:
                        continue  # not a shape we can call volatile: start a new conversation
                    conversation["volatile"].append(
                        {"index": volatile_index, "kind": kind, "text": text}
                    )
                conversation["requests"].append(messages)
                return conversation, len(conversation["requests"]), volatile_index
        conversation = {
            "id": f"conv_{len(self._conversations) + 1:02d}",
            "requests": [messages],
            "volatile": [],
        }
        self._conversations.append(conversation)
        return conversation, 1, None

    # -- the index ---------------------------------------------------------

    def build_index(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)
            conversations = [
                {
                    "id": c["id"],
                    "requests": len(c["requests"]),
                    "volatile_suffix": c["volatile"][-1] if c["volatile"] else None,
                }
                for c in self._conversations
            ]
        models = _unique(_get(r, "body", "model") for r in records)
        tools: list[str] = []
        for record in records:
            for tool in (record.get("body") or {}).get("tools") or []:
                name = tool.get("name") if isinstance(tool, dict) else None
                if isinstance(name, str) and name not in tools:
                    tools.append(name)
        params: dict[str, list[Any]] = {}
        for key in ("tool_choice", "thinking", "output_config", "temperature", "top_p", "top_k",
                    "max_tokens", "stream", "service_tier"):
            seen = _unique(_get(r, "body", key) for r in records)
            if seen:
                params[key] = seen
        return {
            "upshift_version": __version__,
            "created_at": self.created_at,
            "closed_at": datetime.now(UTC).isoformat(),
            "mode": self.mode,
            "listen": self.listen,
            "upstream": self.upstream if self.mode == "forward" else None,
            "framework": detect.summarize(records, self.framework),
            "conversations": len(conversations),
            "requests": len(records),
            "conversation_index": conversations,
            "models": models,
            "tools": sorted(tools),
            "params_seen": params,
        }

    def close(self) -> Path:
        path = self.out_dir / "index.json"
        _write_json(path, self.build_index())
        return path


def _get(record: dict[str, Any], *keys: str) -> Any:
    node: Any = record
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _unique(values) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if value is None:
            continue
        if value not in out:
            out.append(value)
    return out


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Reading a capture back
# ---------------------------------------------------------------------------


def load_capture(capture_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(index.json, [conversation, ...]) for a finished capture.

    A conversation is `{"id", "volatile_suffix", "turns": [{"request", "response"}, ...]}` in
    request order. Raises ValueError with a one-line reason when the directory is not a
    capture — every failure a user can cause is a clean message, as everywhere else.
    """
    root = Path(capture_dir)
    index_path = root / "index.json"
    if not index_path.is_file():
        raise ValueError(
            f"{index_path} not found — {root} is not an upshift capture directory "
            f"(a capture writes index.json when it shuts down; stop the recorder with Ctrl-C)"
        )
    try:
        index = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{index_path}: cannot be read ({exc})") from exc

    conversations_dir = root / "conversations"
    conversations: list[dict[str, Any]] = []
    volatile_by_id = {
        entry.get("id"): entry.get("volatile_suffix")
        for entry in index.get("conversation_index") or []
        if isinstance(entry, dict)
    }
    directories = sorted(
        (d for d in conversations_dir.iterdir() if d.is_dir()) if conversations_dir.is_dir() else [],
        key=lambda d: (_conversation_order(d.name), d.name),
    )
    for directory in directories:
        turns = []
        for request_path in sorted(directory.glob("req_*.json")):
            response_path = directory / request_path.name.replace("req_", "res_", 1)
            request = json.loads(request_path.read_text())
            response = json.loads(response_path.read_text()) if response_path.is_file() else None
            turns.append({"request": request, "response": response})
        if turns:
            conversations.append(
                {
                    "id": directory.name,
                    "volatile_suffix": volatile_by_id.get(directory.name),
                    "turns": turns,
                }
            )
    if not conversations:
        raise ValueError(f"{root} contains no recorded conversation (nothing to adapt)")
    return index, conversations


def _conversation_order(name: str) -> int:
    match = _CONVERSATION_RE.match(name)
    return int(match.group(1)) if match else 1_000_000
