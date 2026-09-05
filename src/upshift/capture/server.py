"""The local forwarding recorder behind `upshift capture`.

The user exports `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` (or the framework's own base-URL
setting — README, "Framework agents (capture mode)") and runs their agent as usual. Every
request is passed upstream with the caller's own credentials, the response is passed back
untouched, and both are written to the capture directory.

Design constraints, in the order they constrain the code:

* **Transparent.** The framework must not be able to tell the difference, so the upstream
  status, body and content type are returned verbatim — including a 400. A recorder that
  swallowed the error would hide the exact thing upshift exists to catch.
* **Loopback by default.** The proxy handles other people's API keys; it binds 127.0.0.1
  unless the operator passes `--allow-remote`, and it never writes a credential to disk
  (`record.redact_headers`).
* **No new dependency.** stdlib `http.server` + `urllib.request`. A recorder that needs a web
  framework is a recorder people will not run in the environment their agent already runs in.

Streaming: an SSE response is relayed chunk by chunk as it arrives (the framework sees its own
stream, at its own speed) while a copy is accumulated, parsed and reassembled into the message
the events add up to. Both go into the record.
"""

from __future__ import annotations

import json
import signal
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from upshift.capture.record import DEFAULT_MAX_BODY_BYTES, CaptureStore
from upshift.capture.sse import parse_events, reassemble

DEFAULT_LISTEN = "127.0.0.1:8787"
DEFAULT_UPSTREAM = "https://api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
UPSTREAM_TIMEOUT_S = 900.0
CHUNK = 8192

#: Hop-by-hop headers, plus the two that must be recomputed for the forwarded request.
#: `accept-encoding` is forced to identity so the recorded body is the body, not gzip.
_DROP_REQUEST_HEADERS = frozenset(
    {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade",
     "te", "trailer", "content-length", "accept-encoding"}
)
_DROP_RESPONSE_HEADERS = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
)

#: Loopback addresses `--allow-remote` is not needed for.
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class CaptureConfig:
    """Everything the handler needs, resolved once."""

    def __init__(
        self,
        *,
        store: CaptureStore,
        upstream: str,
        sim: bool,
        sim_cases: dict[str, dict[str, Any]] | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        on_record: Callable[[str, int, int | None, str], None] | None = None,
    ) -> None:
        self.store = store
        self.upstream = upstream.rstrip("/")
        self.sim = sim
        self.sim_cases = sim_cases or {}
        self.max_body_bytes = max_body_bytes
        self.on_record = on_record
        self._sim_provider: Any | None = None

    def sim_provider(self) -> Any:
        if self._sim_provider is None:
            from upshift.providers.sim import SimProvider

            self._sim_provider = SimProvider()
        return self._sim_provider


def parse_listen(listen: str, *, allow_remote: bool) -> tuple[str, int]:
    """'host:port' -> (host, port), refusing a non-loopback bind without --allow-remote."""
    text = str(listen or "").strip()
    host, _, port_text = text.rpartition(":")
    if not host or not port_text:
        raise ValueError(f"--listen must be host:port (got {listen!r})")
    host = host.strip("[]")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"--listen port must be a number (got {port_text!r})") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"--listen port out of range: {port}")
    if host not in LOOPBACK and not allow_remote:
        raise ValueError(
            f"refusing to bind {host}:{port}: this recorder handles your API key and your "
            f"agent's traffic, so it listens on loopback only. Pass --allow-remote if you "
            f"really mean to expose it on the network."
        )
    return host, port


def check_upstream(upstream: str) -> str:
    parts = urlsplit(upstream)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"--upstream must be an http(s) URL (got {upstream!r})")
    return upstream.rstrip("/")


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "upshift-capture"
    sys_version = ""

    #: set on the server object by run_capture
    config: CaptureConfig

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the default stderr access log; `on_record` is the user-facing progress."""

    # -- entry points ------------------------------------------------------

    def do_POST(self) -> None:
        body_bytes, too_large = self._read_body()
        if too_large:
            self._oversized(body_bytes)
            return
        if self._recordable():
            self._handle_messages(body_bytes)
        else:
            self._forward_only("POST", body_bytes)

    def do_GET(self) -> None:
        self._forward_only("GET", b"")

    def do_DELETE(self) -> None:
        self._forward_only("DELETE", b"")

    # -- helpers -----------------------------------------------------------

    @property
    def _config(self) -> CaptureConfig:
        return self.server.config  # type: ignore[attr-defined]

    def _recordable(self) -> bool:
        return self.path.split("?", 1)[0].rstrip("/").endswith(MESSAGES_PATH)

    def _read_body(self) -> tuple[bytes, bool]:
        limit = self._config.max_body_bytes
        if (self.headers.get("transfer-encoding") or "").lower() == "chunked":
            return self._read_chunked(limit)
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            self.rfile.read(min(length, limit + 1))
            return b"", True
        return self.rfile.read(length) if length else b"", False

    def _read_chunked(self, limit: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(64).strip()
            try:
                size = int(line.split(b";")[0] or b"0", 16)
            except ValueError:
                return b"".join(chunks), False
            if size == 0:
                self.rfile.readline(8)
                break
            total += size
            if total > limit:
                return b"", True
            chunks.append(self.rfile.read(size))
            self.rfile.readline(8)  # trailing CRLF
        return b"".join(chunks), False

    def _upstream_headers(self) -> dict[str, str]:
        """The caller's own headers, minus hop-by-hop. Credentials pass through untouched:
        upshift never holds them, never writes them, and never substitutes its own."""
        out: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in _DROP_REQUEST_HEADERS:
                continue
            out[key] = value
        out["Accept-Encoding"] = "identity"
        return out

    def _url(self) -> str:
        return f"{self._config.upstream}{self.path}"

    # -- forwarding --------------------------------------------------------

    def _forward_only(self, method: str, body: bytes) -> None:
        """A path upshift does not record (models, count_tokens, batches): relay it."""
        if self._config.sim:
            self._sim_side_channel(method)
            return
        try:
            status, headers, payload = self._fetch(method, body)
        except OSError as exc:
            self._send_json(502, _gateway_error(exc))
            return
        self._send_raw(status, headers, payload)

    def _fetch(self, method: str, body: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
        request = urllib.request.Request(
            self._url(), data=body or None, headers=self._upstream_headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S) as response:
                return response.status, list(response.headers.items()), response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, list(error.headers.items()), error.read()

    # -- the recorded path -------------------------------------------------

    def _handle_messages(self, body_bytes: bytes) -> None:
        try:
            body: Any = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
        except (ValueError, UnicodeDecodeError):
            body = {"_unparsed": True, "bytes": len(body_bytes)}
        wants_stream = bool(isinstance(body, dict) and body.get("stream"))
        started = time.monotonic()

        if self._config.sim:
            if wants_stream:
                # The simulator has no stream, and inventing SSE frames would put bytes in a
                # capture that no model ever sent. Say so instead.
                message = (
                    "capture --sim cannot answer a streaming request: the bundled simulator "
                    "emits whole messages, and upshift will not synthesise SSE frames into a "
                    "capture. Turn streaming off in your framework for the sim demo, or drop "
                    "--sim and record against the real API (streaming is fully supported "
                    "there)."
                )
                self._send_json(400, _api_error(message))
                self._record(body, len(body_bytes), 400, _api_error(message), None, False,
                             time.monotonic() - started, message)
                return
            status, response_body, events, error, raw = self._sim_messages(body)
            self._send_json(status, response_body if status == 200 else raw)
            self._record(body, len(body_bytes), status, response_body, events, False,
                         time.monotonic() - started, error)
            return

        try:
            status, payload, events, streamed = self._relay(body_bytes, wants_stream)
        except OSError as exc:
            payload_body = _gateway_error(exc)
            self._send_json(502, payload_body)
            self._record(body, len(body_bytes), None, payload_body, None, False,
                         time.monotonic() - started, f"{type(exc).__name__}: {exc}")
            return

        latency = time.monotonic() - started
        if streamed:
            recorded_body = reassemble(events or [])
            error = None if recorded_body else "stream produced no message_start event"
        else:
            recorded_body, error = _decode(payload)
        self._record(body, len(body_bytes), status, recorded_body, events, streamed, latency,
                     error)

    def _relay(
        self, body_bytes: bytes, wants_stream: bool
    ) -> tuple[int, bytes, list[dict[str, Any]] | None, bool]:
        """Send upstream and relay the answer to the framework as it arrives."""
        request = urllib.request.Request(
            self._url(), data=body_bytes, headers=self._upstream_headers(), method="POST"
        )
        try:
            response = urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            with error:
                payload = error.read()
                self._send_raw(error.code, list(error.headers.items()), payload)
                return error.code, payload, None, False
        with response:
            headers = list(response.headers.items())
            is_sse = "text/event-stream" in (response.headers.get("content-type") or "").lower()
            if not (wants_stream and is_sse):
                payload = response.read()
                self._send_raw(response.status, headers, payload)
                return response.status, payload, None, False
            text = self._stream(response, response.status, headers)
        return response.status, b"", parse_events(text), True

    def _stream(self, response: Any, status: int, headers: list[tuple[str, str]]) -> str:
        """Relay an SSE body chunk by chunk, keeping a copy for reassembly."""
        self._send_head(status, headers, streaming=True)
        copied: list[bytes] = []
        total = 0
        limit = self._config.max_body_bytes
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break  # the framework hung up; keep what we already recorded
            total += len(chunk)
            if total <= limit:
                copied.append(chunk)
        self.close_connection = True
        return b"".join(copied).decode("utf-8", errors="replace")

    def _record(
        self,
        body: Any,
        raw_bytes: int,
        status: int | None,
        response_body: Any,
        events: list[dict[str, Any]] | None,
        streamed: bool,
        latency_s: float,
        error: str | None,
    ) -> None:
        conversation, index = self._config.store.add(
            headers=self.headers,
            body=body,
            raw_body_bytes=raw_bytes,
            path=self.path,
            status=status,
            response_body=response_body,
            events=events,
            streamed=streamed,
            latency_s=latency_s,
            error=error,
        )
        if self._config.on_record:
            self._config.on_record(conversation, index, status, error or "")

    # -- sim mode ----------------------------------------------------------

    def _sim_messages(
        self, body: Any
    ) -> tuple[int, Any, list[dict[str, Any]] | None, str | None, Any]:
        """Answer from the bundled simulator instead of forwarding. $0, no key, no network."""
        from upshift.providers.base import ProviderAPIError

        case = _match_case(body, self._config.sim_cases)
        if case is None:
            error = (
                "capture --sim needs an oracle plan for this conversation: no case in "
                "--sim-agent matches this request's first user message. Add the case, or "
                "drop --sim and record against the real API."
            )
            return 400, None, None, error, _api_error(error)
        conversation_key = case["id"]
        turn = _turn_index(body)
        try:
            response = self._config.sim_provider().call(
                "messages",
                body,
                f"{conversation_key}:0:{turn}",
                {"case_id": conversation_key, "rep": 0, "sim": case.get("sim") or {}},
            )
        except ProviderAPIError as exc:
            return exc.status_code or 400, None, None, exc.message, _api_error(exc.message)
        except ValueError as exc:
            return 400, None, None, str(exc), _api_error(str(exc))
        return 200, response, None, None, response

    def _sim_side_channel(self, method: str) -> None:
        """GET /v1/models/<id> in sim mode, so a preflight does not have to be special-cased.

        Everything it reports is clearly a simulator's answer; nothing here talks to a model.
        """
        path = self.path.split("?", 1)[0].rstrip("/")
        if method == "GET" and "/v1/models/" in path:
            model_id = path.rsplit("/", 1)[-1]
            self._send_json(200, {
                "id": model_id,
                "type": "model",
                "display_name": f"{model_id} (upshift capture --sim)",
                "capabilities": {"effort": {"levels": ["low", "medium", "high", "xhigh", "max"]}},
            })
            return
        self._send_json(404, _api_error(
            f"capture --sim serves {MESSAGES_PATH} and GET /v1/models/<id> only; "
            f"{method} {self.path} was not forwarded because --sim never touches the network"
        ))

    # -- responses ---------------------------------------------------------

    def _oversized(self, _body: bytes) -> None:
        limit = self._config.max_body_bytes
        message = (
            f"request body exceeds upshift capture's --max-body-bytes ({limit} bytes) and was "
            f"neither forwarded nor recorded"
        )
        self._send_json(413, _api_error(message))

    def _send_head(self, status: int, headers: list[tuple[str, str]], *, streaming: bool) -> None:
        self.send_response(status)
        for key, value in headers:
            if key.lower() in _DROP_RESPONSE_HEADERS:
                continue
            self.send_header(key, value)
        if streaming:
            # No length is knowable up front; the framework reads to EOF (the Anthropic SDKs
            # and every httpx/fetch client handle close-delimited SSE).
            self.send_header("Connection", "close")
        self.end_headers()

    def _send_raw(self, status: int, headers: list[tuple[str, str]], payload: bytes) -> None:
        self.send_response(status)
        for key, value in headers:
            if key.lower() in _DROP_RESPONSE_HEADERS:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, status: int, payload: Any) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        try:
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _decode(payload: bytes) -> tuple[Any, str | None]:
    try:
        return json.loads(payload.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError) as exc:
        return {"_unparsed": True, "bytes": len(payload)}, f"response was not JSON: {exc}"


def _api_error(message: str) -> dict[str, Any]:
    """An error shaped the way the Messages API shapes one, so a framework's own error
    handling sees what it expects."""
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def _gateway_error(exc: BaseException) -> dict[str, Any]:
    return _api_error(
        f"upshift capture could not reach the upstream API ({type(exc).__name__}: {exc})"
    )


def _first_user_text(body: Any) -> str:
    for message in (body or {}).get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(b.get("text") or "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
    return ""


def _match_case(body: Any, cases: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The `--sim-agent` case whose first user message this request starts with."""
    text = _first_user_text(body).strip()
    if not text:
        return None
    return cases.get(text)


def _turn_index(body: Any) -> int:
    """How many assistant turns this request already carries — the sim's call index."""
    return sum(
        1
        for message in (body or {}).get("messages") or []
        if isinstance(message, dict) and message.get("role") == "assistant"
    )


def load_sim_cases(agent_dir: str | Path) -> dict[str, dict[str, Any]]:
    """{first user message -> case} for `--sim-agent`, so the simulator has an oracle plan."""
    from upshift.schemas import Case

    path = Path(agent_dir) / "cases" / "cases.json"
    if not path.is_file():
        raise ValueError(f"--sim-agent {agent_dir}: no cases/cases.json (see ADAPTER.md)")
    out: dict[str, dict[str, Any]] = {}
    for case in Case.load_all(path):
        if case.user_messages:
            out[str(case.user_messages[0]).strip()] = {"id": case.id, "sim": case.sim}
    return out


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def run_capture(
    out_dir: str | Path,
    *,
    listen: str = DEFAULT_LISTEN,
    upstream: str = DEFAULT_UPSTREAM,
    allow_remote: bool = False,
    framework: str | None = None,
    sim: bool = False,
    sim_agent: str | Path | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    on_record: Callable[[str, int, int | None, str], None] | None = None,
    on_ready: Callable[[str, int], None] | None = None,
    stop: threading.Event | None = None,
) -> Path:
    """Record until SIGINT (or `stop` is set); returns the written index.json path.

    Blocks. The HTTP loop runs on its own thread so a Ctrl-C in the main thread can shut the
    server down cleanly and still write index.json — an interrupted capture that lost its
    index would be a capture nothing can adapt.
    """
    host, port = parse_listen(listen, allow_remote=allow_remote)
    upstream = check_upstream(upstream)
    sim_cases = load_sim_cases(sim_agent) if sim_agent else {}
    if sim_agent and not sim:
        raise ValueError("--sim-agent only means something with --sim")

    store = CaptureStore(
        out_dir,
        listen=f"{host}:{port}",
        upstream=upstream,
        mode="sim" if sim else "forward",
        framework=framework,
        max_body_bytes=max_body_bytes,
    )
    config = CaptureConfig(
        store=store,
        upstream=upstream,
        sim=sim,
        sim_cases=sim_cases,
        max_body_bytes=max_body_bytes,
        on_record=on_record,
    )
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.config = config  # type: ignore[attr-defined]

    finished = stop or threading.Event()
    previous = signal.getsignal(signal.SIGINT)

    def handler(_signum: int, _frame: Any) -> None:
        finished.set()

    installed = False
    try:
        signal.signal(signal.SIGINT, handler)
        installed = True
    except ValueError:  # not the main thread (tests drive `stop` instead)
        pass

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                              daemon=True)
    thread.start()
    if on_ready:
        on_ready(host, port)
    try:
        while not finished.wait(0.2):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if installed:
            signal.signal(signal.SIGINT, previous)
    return store.close()
