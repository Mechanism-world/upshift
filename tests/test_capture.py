"""`upshift capture`: the recorder, against a fake upstream. No network, no key, no cost.

Every test here drives the real proxy over a real socket — the point of a wire recorder is
what happens on the wire, so a test that called the handler's methods directly would test the
wrong thing.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from upshift.capture import detect
from upshift.capture.record import (
    REDACTED,
    CaptureStore,
    extends,
    load_capture,
    redact_headers,
    volatile_part,
)
from upshift.capture.server import parse_listen, run_capture
from upshift.capture.sse import parse_events, reassemble

ROOT = Path(__file__).resolve().parents[1]
COOKBOOK_SMS = ROOT / "agents" / "cookbook-sms"

SDK_HEADERS = {
    "content-type": "application/json",
    "x-api-key": "sk-ant-api03-THIS-MUST-NEVER-REACH-DISK",
    "user-agent": "Anthropic/Python 1.4.0",
    "x-stainless-lang": "python",
    "x-stainless-package-version": "1.4.0",
    "anthropic-version": "2023-06-01",
}

TEXT_MESSAGE = {
    "id": "msg_text",
    "type": "message",
    "role": "assistant",
    "model": "claude-fable-5",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 3},
}

SSE_FRAMES = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_stream", "type": "message", "role": "assistant", "model": "claude-fable-5",
        "content": [], "stop_reason": None, "usage": {"input_tokens": 9, "output_tokens": 0}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "input_json_delta", "partial_json": '{"city"'}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "input_json_delta", "partial_json": ': "Paris"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                       "usage": {"output_tokens": 12}}),
    ("message_stop", {"type": "message_stop"}),
]

FORCED_TOOL_CHOICE_400 = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
    },
}


# ---------------------------------------------------------------------------
# A fake api.anthropic.com
# ---------------------------------------------------------------------------


def _lower(headers) -> dict[str, str]:
    """HTTP header names are case-insensitive and urllib re-cases them on the way out."""
    return {key.lower(): value for key, value in headers.items()}


class _FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.server.seen.append({"method": "GET", "path": self.path,
                                 "headers": _lower(self.headers)})
        self._json(200, {"id": self.path.rsplit("/", 1)[-1], "type": "model"})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append({"method": "POST", "path": self.path, "body": body,
                                 "headers": _lower(self.headers)})
        if body.get("tool_choice", {}).get("type") in ("tool", "any"):
            self._json(400, FORCED_TOOL_CHOICE_400)
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for name, data in SSE_FRAMES:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True
            return
        self._json(200, TEXT_MESSAGE)

    def _json(self, status, payload):
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class _Recorder:
    """A running `upshift capture`, on a free port."""

    def __init__(self, out_dir, **kwargs):
        self.out_dir = Path(out_dir)
        self.kwargs = kwargs
        self.stop = threading.Event()
        self._ready = threading.Event()
        self.port = None
        self.index_path = None
        self.error = None

    def __enter__(self):
        def serve():
            try:
                self.index_path = run_capture(
                    self.out_dir, listen="127.0.0.1:0", stop=self.stop,
                    on_ready=self._on_ready, **self.kwargs
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions below
                self.error = exc
                self._ready.set()

        self._thread = threading.Thread(target=serve)
        self._thread.start()
        assert self._ready.wait(10), "recorder never started"
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self._thread.join(15)
        return False

    def _on_ready(self, host, port):
        self.port = port
        self._ready.set()

    def post(self, body, headers=None, path="/v1/messages"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers=headers if headers is not None else dict(SDK_HEADERS),
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()


def ask(text, **extra):
    body = {"model": "claude-fable-5", "max_tokens": 64,
            "messages": [{"role": "user", "content": text}]}
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# Forwarding and recording
# ---------------------------------------------------------------------------


def test_forwards_and_records_a_plain_request(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        status, payload = recorder.post(ask("hello"))
    assert status == 200
    assert json.loads(payload) == TEXT_MESSAGE
    index, conversations = load_capture(tmp_path / "cap")
    assert index["requests"] == 1
    assert index["conversations"] == 1
    assert index["models"] == ["claude-fable-5"]
    turn = conversations[0]["turns"][0]
    assert turn["request"]["body"]["messages"][0]["content"] == "hello"
    assert turn["response"]["status"] == 200
    assert turn["response"]["body"] == TEXT_MESSAGE
    assert turn["response"]["latency_s"] >= 0


def test_the_api_key_reaches_upstream_and_never_reaches_disk(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        recorder.post(ask("hello"))
    # the caller's own credential is what authenticated the upstream call
    assert upstream.seen[0]["headers"]["x-api-key"] == SDK_HEADERS["x-api-key"]
    # ...and it is nowhere in the capture directory
    for path in (tmp_path / "cap").rglob("*.json"):
        assert "sk-ant-api03" not in path.read_text()
    _, conversations = load_capture(tmp_path / "cap")
    assert conversations[0]["turns"][0]["request"]["headers"]["x-api-key"] == REDACTED


def test_an_upstream_400_is_relayed_verbatim_and_recorded(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        status, payload = recorder.post(ask("hi", tool_choice={"type": "any"}))
    assert status == 400
    assert json.loads(payload) == FORCED_TOOL_CHOICE_400
    _, conversations = load_capture(tmp_path / "cap")
    response = conversations[0]["turns"][0]["response"]
    assert response["status"] == 400
    assert "not supported for this model" in response["body"]["error"]["message"]


def test_a_streaming_response_is_relayed_and_reassembled(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        status, payload = recorder.post(ask("weather?", stream=True))
    assert status == 200
    assert payload.startswith(b"event: message_start")  # the framework got its own stream
    _, conversations = load_capture(tmp_path / "cap")
    response = conversations[0]["turns"][0]["response"]
    assert response["streamed"] is True
    assert [e["event"] for e in response["events"]][:2] == ["message_start", "content_block_start"]
    assert response["body"]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}
    ]
    assert response["body"]["stop_reason"] == "tool_use"
    assert response["body"]["usage"]["output_tokens"] == 12


def test_an_unversioned_messages_path_is_recorded_and_versioned_upstream(tmp_path, upstream):
    """@ai-sdk/anthropic (so also opencode) requests `${baseURL}/messages`, without the /v1."""
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        status, _ = recorder.post(ask("hello"), path="/messages")
    assert status == 200
    assert upstream.seen[0]["path"] == "/v1/messages"
    index, conversations = load_capture(tmp_path / "cap")
    assert index["requests"] == 1
    assert conversations[0]["turns"][0]["request"]["path"] == "/messages"


def test_a_query_string_still_counts_as_the_messages_path(tmp_path, upstream):
    """pydantic-ai and the Claude Agent SDK both POST /v1/messages?beta=true."""
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        status, _ = recorder.post(ask("hello"), path="/v1/messages?beta=true")
    assert status == 200
    index, conversations = load_capture(tmp_path / "cap")
    assert index["requests"] == 1
    assert conversations[0]["turns"][0]["request"]["path"] == "/v1/messages?beta=true"


def test_other_paths_are_forwarded_but_not_recorded(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        status, payload = recorder.get("/v1/models/claude-fable-5")
        recorder.post(ask("hello"))
    assert status == 200
    assert json.loads(payload)["id"] == "claude-fable-5"
    assert [entry["method"] for entry in upstream.seen] == ["GET", "POST"]
    index, _ = load_capture(tmp_path / "cap")
    assert index["requests"] == 1  # only the messages call


def test_an_unreachable_upstream_is_a_502_and_is_recorded(tmp_path):
    with _Recorder(tmp_path / "cap", upstream="http://127.0.0.1:1") as recorder:
        status, payload = recorder.post(ask("hello"))
    assert status == 502
    assert "could not reach the upstream" in json.loads(payload)["error"]["message"]
    _, conversations = load_capture(tmp_path / "cap")
    assert conversations[0]["turns"][0]["response"]["status"] is None
    assert conversations[0]["turns"][0]["response"]["error"]


def test_an_oversized_body_is_refused_rather_than_half_recorded(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream), max_body_bytes=2048) as recorder:
        status, payload = recorder.post(ask("x" * 5000))
    assert status == 413
    assert "max-body-bytes" in json.loads(payload)["error"]["message"]
    assert upstream.seen == []  # nothing forwarded
    index = json.loads((tmp_path / "cap" / "index.json").read_text())
    assert index["requests"] == 0  # and nothing recorded


def test_the_index_summarises_params_tools_and_framework(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        recorder.post(ask("hello", tools=[{"name": "get_weather", "description": "w",
                                           "input_schema": {"type": "object"}}],
                          temperature=0.7))
    index, _ = load_capture(tmp_path / "cap")
    assert index["tools"] == ["get_weather"]
    assert index["params_seen"]["temperature"] == [0.7]
    assert index["params_seen"]["max_tokens"] == [64]
    assert index["framework"]["framework"] == "anthropic-sdk-python"
    assert index["framework"]["user_agents"] == ["Anthropic/Python 1.4.0"]


def test_the_framework_can_be_named_instead_of_detected(tmp_path, upstream):
    with _Recorder(tmp_path / "cap", upstream=_url(upstream), framework="opencode") as recorder:
        recorder.post(ask("hello"))
    index, _ = load_capture(tmp_path / "cap")
    assert index["framework"]["framework"] == "opencode"
    assert index["framework"]["override"] == "opencode"
    assert index["framework"]["detected"] == ["anthropic-sdk-python"]  # what the headers said


# ---------------------------------------------------------------------------
# Conversation grouping
# ---------------------------------------------------------------------------


def test_a_tool_loop_is_one_conversation_and_a_new_question_is_another(tmp_path, upstream):
    first = ask("weather?")
    follow_up = ask("weather?")
    follow_up["messages"] = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1",
                                           "name": "get_weather", "input": {"city": "Paris"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                      "content": '{"c": 20}'}]},
    ]
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        recorder.post(first)
        recorder.post(follow_up)
        recorder.post(ask("something else entirely"))
    index, conversations = load_capture(tmp_path / "cap")
    assert index["conversations"] == 2
    assert [len(c["turns"]) for c in conversations] == [2, 1]


def test_extends_requires_a_strict_prefix():
    a = [{"role": "user", "content": "one"}]
    b = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    assert extends(a, b) == (True, None)
    assert extends(a, a) == (False, None)  # same length is not a continuation
    assert extends(b, a) == (False, None)
    assert extends(a, [{"role": "user", "content": "other"}, {"role": "assistant", "c": 1}]) == (
        False, None
    )


def test_a_regenerated_trailing_user_message_is_a_volatile_suffix_not_a_new_conversation(
    tmp_path, upstream
):
    def request(facts, tail=()):
        body = ask("what time is it?")
        body["messages"] = [
            {"role": "user", "content": "what time is it?"},
            {"role": "user", "content": f"<facts>current_time: {facts}</facts>"},
            *tail,
        ]
        return body

    tail = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_9",
                                           "name": "clock", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9",
                                      "content": "12:00"}]},
    ]
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        recorder.post(request("12:00:01"))
        recorder.post(request("12:00:02", tail))
    index, conversations = load_capture(tmp_path / "cap")
    assert index["conversations"] == 1
    volatile = conversations[0]["volatile_suffix"]
    assert volatile["kind"] == "message"
    assert volatile["index"] == 1
    assert volatile["text"] == "<facts>current_time: 12:00:02</facts>"


def test_volatile_part_reads_a_trailing_block_when_the_earlier_blocks_match():
    previous = {"role": "user", "content": [{"type": "text", "text": "question"},
                                            {"type": "text", "text": "now: 1"}]}
    current = {"role": "user", "content": [{"type": "text", "text": "question"},
                                           {"type": "text", "text": "now: 2"}]}
    assert volatile_part(previous, current) == ("block", "now: 2")


def test_volatile_part_refuses_a_shape_it_cannot_read():
    previous = {"role": "user", "content": [{"type": "image", "source": {}}]}
    current = {"role": "user", "content": [{"type": "image", "source": {"a": 1}}]}
    assert volatile_part(previous, current) == ("unknown", None)


# ---------------------------------------------------------------------------
# Redaction and detection, as units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    ["x-api-key", "X-API-Key", "authorization", "Proxy-Authorization", "cookie",
     "x-some-vendor-token", "openai-api-key", "x-refresh-secret"],
)
def test_every_credential_shaped_header_is_redacted(header):
    assert redact_headers({header: "the-secret"})[header.lower()] == REDACTED


def test_headers_outside_the_allowlist_are_dropped_entirely():
    recorded = redact_headers({"user-agent": "x", "x-stainless-lang": "python",
                               "x-forwarded-for": "10.0.0.1", "host": "api.anthropic.com"})
    assert recorded == {"user-agent": "x", "x-stainless-lang": "python"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"user-agent": "pydantic-ai/2.40.0", "x-stainless-lang": "python"}, "pydantic-ai"),
        ({"user-agent": "claude-cli/2.1.259 (external, sdk-py)", "x-app": "cli"},
         "claude-agent-sdk"),
        ({"user-agent": "litellm/1.83.9"}, "litellm"),
        ({"user-agent": "opencode/0.15.2 ai-sdk/provider-utils/4.0.46 runtime/bun/1.2"},
         "opencode"),
        ({"user-agent": "ai/7.0.93 ai-sdk/provider-utils/5.0.36 runtime/node.js/v22"},
         "vercel-ai-sdk"),
        # langchain-anthropic adds no header of its own: on the wire it IS the Python SDK.
        ({"user-agent": "Anthropic/Python 0.125.0", "x-stainless-lang": "python"},
         "anthropic-sdk-python"),
        ({"user-agent": "Anthropic/Python 1.4.0", "x-stainless-lang": "python"},
         "anthropic-sdk-python"),
        ({"user-agent": "Anthropic/JS 0.124.0", "x-stainless-lang": "js"},
         "anthropic-sdk-typescript"),
        ({"user-agent": "curl/8.0"}, detect.UNKNOWN),
    ],
)
def test_framework_detection_prefers_the_specific_name(headers, expected):
    assert detect.detect(headers)[0] == expected


# ---------------------------------------------------------------------------
# SSE, as a unit
# ---------------------------------------------------------------------------


def test_parse_events_tolerates_a_frame_that_is_not_json():
    events = parse_events("event: ping\ndata: {}\n\nevent: junk\ndata: not json\n\n")
    assert events[0]["event"] == "ping"
    assert events[1]["data"] == {"_raw": "not json"}


def test_reassemble_accumulates_text_and_thinking_blocks():
    events = parse_events(
        "".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n"
            for name, data in [
                ("message_start", {"message": {"id": "m", "content": [], "usage": {}}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "thinking",
                                                                       "thinking": ""}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta",
                                                               "thinking": "step "}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "signature_delta",
                                                               "signature": "sig"}}),
                ("content_block_stop", {"index": 0}),
                ("content_block_start", {"index": 1, "content_block": {"type": "text",
                                                                       "text": ""}}),
                ("content_block_delta", {"index": 1, "delta": {"type": "text_delta",
                                                               "text": "ans"}}),
                ("content_block_stop", {"index": 1}),
                ("message_delta", {"delta": {"stop_reason": "end_turn"}}),
            ]
        )
    )
    message = reassemble(events)
    assert message["content"] == [
        {"type": "thinking", "thinking": "step ", "signature": "sig"},
        {"type": "text", "text": "ans"},
    ]
    assert message["stop_reason"] == "end_turn"


def test_reassemble_keeps_a_truncated_tool_input_visible():
    events = parse_events(
        "".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n"
            for name, data in [
                ("message_start", {"message": {"id": "m", "content": []}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "tool_use",
                                                                       "name": "t", "input": {}}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta",
                                                               "partial_json": '{"a": '}}),
                ("content_block_stop", {"index": 0}),
            ]
        )
    )
    assert reassemble(events)["content"][0]["_partial_json"] == '{"a": '


def test_reassemble_returns_none_without_a_message_start():
    assert reassemble(parse_events("event: ping\ndata: {}\n\n")) is None


# ---------------------------------------------------------------------------
# Binding rules
# ---------------------------------------------------------------------------


def test_loopback_binds_and_a_public_address_needs_allow_remote():
    assert parse_listen("127.0.0.1:8787", allow_remote=False) == ("127.0.0.1", 8787)
    assert parse_listen("0.0.0.0:8787", allow_remote=True) == ("0.0.0.0", 8787)
    with pytest.raises(ValueError, match="loopback only"):
        parse_listen("0.0.0.0:8787", allow_remote=False)
    with pytest.raises(ValueError, match="host:port"):
        parse_listen("8787", allow_remote=False)
    with pytest.raises(ValueError, match="must be a number"):
        parse_listen("127.0.0.1:http", allow_remote=False)


# ---------------------------------------------------------------------------
# --sim mode
# ---------------------------------------------------------------------------


def test_sim_mode_answers_from_the_simulator_without_a_network(tmp_path):
    body = ask("Hey there! How are you?")
    body["model"] = "sim-fable-5"
    body["system"] = "You are an SMS bot."
    body["tools"] = [{"name": "send_text_to_user", "description": "t",
                      "input_schema": {"type": "object"}}]
    with _Recorder(tmp_path / "cap", sim=True, sim_agent=COOKBOOK_SMS,
                   upstream="http://127.0.0.1:1") as recorder:
        status, payload = recorder.post(body)
    assert status == 200
    message = json.loads(payload)
    assert message["content"][0]["name"] == "send_text_to_user"
    index, _ = load_capture(tmp_path / "cap")
    assert index["mode"] == "sim"
    assert index["upstream"] is None


def test_sim_mode_says_so_when_no_case_supplies_an_oracle_plan(tmp_path):
    body = ask("a question no cookbook-sms case asks")
    body["model"] = "sim-fable-5"
    with _Recorder(tmp_path / "cap", sim=True, sim_agent=COOKBOOK_SMS) as recorder:
        status, payload = recorder.post(body)
    assert status == 400
    assert "no case in --sim-agent matches" in json.loads(payload)["error"]["message"]


def test_sim_mode_refuses_to_invent_sse_frames(tmp_path):
    body = ask("Hey there! How are you?")
    body["model"] = "sim-fable-5"
    body["stream"] = True
    with _Recorder(tmp_path / "cap", sim=True, sim_agent=COOKBOOK_SMS) as recorder:
        status, payload = recorder.post(body)
    assert status == 400
    assert "will not synthesise SSE frames" in json.loads(payload)["error"]["message"]


def test_sim_mode_serves_a_model_preflight_without_touching_the_network(tmp_path):
    with _Recorder(tmp_path / "cap", sim=True) as recorder:
        status, payload = recorder.get("/v1/models/sim-fable-5")
    assert status == 200
    assert "capture --sim" in json.loads(payload)["display_name"]


def test_sim_agent_without_sim_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="only means something with --sim"):
        run_capture(tmp_path / "cap", sim_agent=COOKBOOK_SMS, stop=threading.Event())


# ---------------------------------------------------------------------------
# Reading a capture back
# ---------------------------------------------------------------------------


def test_load_capture_says_what_is_missing(tmp_path):
    with pytest.raises(ValueError, match="not an upshift capture directory"):
        load_capture(tmp_path)
    (tmp_path / "index.json").write_text("{}")
    with pytest.raises(ValueError, match="no recorded conversation"):
        load_capture(tmp_path)


def test_an_interrupted_capture_still_writes_its_index(tmp_path, upstream):
    """Ctrl-C during a capture must leave something adaptable behind."""
    with _Recorder(tmp_path / "cap", upstream=_url(upstream)) as recorder:
        recorder.post(ask("hello"))
    assert (tmp_path / "cap" / "index.json").is_file()


def test_the_store_writes_one_file_pair_per_request(tmp_path):
    store = CaptureStore(tmp_path / "cap", listen="127.0.0.1:0", upstream="x", mode="forward")
    store.add(headers={}, body={"messages": [{"role": "user", "content": "a"}]},
              raw_body_bytes=10, path="/v1/messages", status=200, response_body=TEXT_MESSAGE,
              events=None, streamed=False, latency_s=0.1)
    store.close()
    files = sorted(p.name for p in (tmp_path / "cap" / "conversations" / "conv_01").iterdir())
    assert files == ["req_01.json", "res_01.json"]


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"
