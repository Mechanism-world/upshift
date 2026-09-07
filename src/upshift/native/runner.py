"""One native episode: run the application's own command, read protocol v1 back.

DESIGN.md §C. `run_native_episode` is the native twin of `agent_loop.run_episode` and returns
the same type, so `upshift.runner.run_suite` builds the same `RepRecord`, `checks.py` evaluates
the same checks, and `differ`/`stats`/`verdict` see one kind of evidence. There is no second
verification engine — that was the requirement the whole native path is shaped around.

What upshift does NOT do here: build the request, choose the params, translate an endpoint, or
repair anything. The application did all of that with its own code, which is the entire value
of a native run — `scope: native_application` (DESIGN.md §A) is the only scope that may be
described as "verified in the application". Repairs stay out (the playbook edits adapter files,
not somebody's source tree), and the report says so.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self

from upshift.agent_loop import EpisodeResult
from upshift.native import protocol
from upshift.native.exec import ExecOutcome, RunnerOptions, run_command
from upshift.native.protocol import CaseRequest, ProtocolError, RunnerSpec
from upshift.schemas import AgentConfig, APICall, Case, ToolExecution


@dataclass
class NativeEpisodeResult(EpisodeResult):
    """`EpisodeResult` plus what only a native run has.

    A subclass rather than new fields on `EpisodeResult`, so the adapter path (owned by the
    translation stream) is untouched and `isinstance(x, EpisodeResult)` still holds everywhere.
    """

    #: DESIGN.md §A companion: how this episode's behaviour was produced.
    episode_source: str = protocol.SOURCE_NATIVE_APPLICATION
    #: Requests the child actually put on the wire, when the run was started with `--capture`.
    wire_requests: list[dict[str, Any]] = field(default_factory=list)
    #: Diagnostics: what ran, how it ended. Always recorded, including on success.
    runner: dict[str, Any] = field(default_factory=dict)


def run_native_episode(
    config: AgentConfig,
    case: Case,
    *,
    rep: int,
    seed: int,
    model: str,
    endpoint: str,
    patch_applied: bool = False,
    options: RunnerOptions | None = None,
    agent_dir: str | Path | None = None,
) -> NativeEpisodeResult:
    """Execute one (case, rep) through the agent's `runner` block."""
    spec = config.runner_spec()
    if spec is None:
        raise ValueError(f"agent {config.name!r} has no `runner` block; this is not a native agent")
    options = options or RunnerOptions()
    request = CaseRequest(
        case_id=case.id,
        rep=rep,
        seed=seed,
        model=model,
        endpoint=endpoint,
        initial_state=case.initial_state,
        user_messages=list(case.user_messages or []),
        patch_applied=patch_applied,
    )
    started = time.monotonic()
    outcome = run_command(
        spec,
        agent_dir if agent_dir is not None else config.agent_dir,
        request.to_json(),
        options,
        request.template_values(),
    )
    result = _to_episode(spec, outcome)
    result.latency_s = round(time.monotonic() - started, 3)
    result.resolved_model = result.resolved_model or model
    return result


def _to_episode(spec: RunnerSpec, outcome: ExecOutcome) -> NativeEpisodeResult:
    result = NativeEpisodeResult()
    result.runner = {
        "command": outcome.command,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "truncated": outcome.truncated,
        "duration_s": outcome.duration_s,
        "isolation": spec.isolation,
        "stderr_tail": _tail(outcome.stderr),
    }
    failure = _harness_failure(spec, outcome)
    if failure is not None:
        result.api_error = protocol.error_payload(failure)
        return result
    try:
        parsed = protocol.parse_result(outcome.stdout)
    except ProtocolError as exc:
        result.api_error = protocol.error_payload(
            f"{exc} (command: {spec.display_command()}; stderr tail: {_tail(outcome.stderr)!r})"
        )
        return result

    result.final_message = parsed.final_message
    result.final_state = parsed.final_state
    result.tool_executions = [
        ToolExecution(
            turn=entry["turn"],
            segment=entry["segment"],
            name=entry["name"],
            arguments=entry["arguments"],
            result=entry["result"],
        )
        for entry in parsed.tool_executions
    ]
    result.api_calls = [
        APICall(
            endpoint="native",
            request=entry["request"],
            response=entry["response"],
            error=entry["error"],
        )
        for entry in parsed.api_calls
    ]
    result.api_error = parsed.api_error
    for key, value in parsed.usage.items():
        result.usage[key] = result.usage.get(key, 0) + value
    for call in result.api_calls:
        model_seen = (call.response or {}).get("model")
        if isinstance(model_seen, str):
            result.resolved_model = model_seen
    return result


def _harness_failure(spec: RunnerSpec, outcome: ExecOutcome) -> str | None:
    """The reasons a native rep says nothing about the model. Order matters: the first true
    thing is the most useful one to report."""
    if outcome.timed_out:
        return (
            f"the runner did not finish within runner.timeout_s={spec.timeout_s:g}s and its "
            f"process group was killed (command: {spec.display_command()}; stderr tail: "
            f"{_tail(outcome.stderr)!r})"
        )
    if outcome.exit_code is None:
        return f"the runner could not be started: {_tail(outcome.stderr)}"
    if outcome.exit_code != 0:
        return (
            f"the runner exited {outcome.exit_code} (command: {spec.display_command()}; "
            f"stderr tail: {_tail(outcome.stderr)!r})"
        )
    if outcome.truncated:
        return (
            f"the runner produced more than runner.max_output_bytes="
            f"{spec.max_output_bytes} bytes, so its output was truncated and the result "
            f"line cannot be trusted"
        )
    return None


def _tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------------------
# `--capture`: the child's own requests, as evidence
# ---------------------------------------------------------------------------


class CaptureSession:
    """The `upshift capture` recorder, running on loopback for the duration of a native run.

    The point (DESIGN.md §C, "Provider-boundary check"): a native run proves the application's
    own code accepts the new model, and the requests that code SERIALIZED are the evidence for
    why. They are attached to each rep record as `wire_requests`.

    Loopback is not a default here, it is the only option: the address handed to the child is
    built from the socket the recorder actually bound, and the requested listen address is
    validated by `capture.server.parse_listen(..., allow_remote=False)`, which refuses anything
    but 127.0.0.1/::1/localhost. `--capture` never grows an `--allow-remote`: a recorder holding
    someone's API key, started implicitly by a test run, must not be reachable off the machine.
    """

    #: Which base-URL variable each provider's SDK reads.
    BASE_URL_VARS: ClassVar[dict[str, str]] = {
        "anthropic": "ANTHROPIC_BASE_URL",
        "openai": "OPENAI_BASE_URL",
        "openai-batch": "OPENAI_BASE_URL",
    }
    UPSTREAMS: ClassVar[dict[str, str]] = {
        "anthropic": "https://api.anthropic.com",
        "openai": "https://api.openai.com",
        "openai-batch": "https://api.openai.com",
    }

    def __init__(
        self,
        out_dir: str | Path,
        *,
        provider: str,
        listen: str = "127.0.0.1:0",
        upstream: str | None = None,
    ) -> None:
        from upshift.capture.server import parse_listen

        self.out_dir = Path(out_dir)
        self.provider = provider
        # Refuses a non-loopback bind. Validated in __init__ so a bad address fails before the
        # run starts, not after the first rep.
        self.host, self.requested_port = parse_listen(listen, allow_remote=False)
        self.upstream = upstream or self.UPSTREAMS.get(provider, "https://api.anthropic.com")
        self.port: int | None = None
        self._thread: Any = None
        self._stop: Any = None
        self._ready: Any = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Self:
        import threading

        from upshift.capture.server import run_capture

        self._stop = threading.Event()
        self._ready = threading.Event()

        def on_ready(host: str, port: int) -> None:
            self.port = port
            self._ready.set()

        def serve() -> None:
            try:
                run_capture(
                    self.out_dir,
                    listen=f"{self.host}:{self.requested_port}",
                    upstream=self.upstream,
                    on_ready=on_ready,
                    stop=self._stop,
                )
            finally:
                self._ready.set()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20) or self.port is None:
            self._stop.set()
            raise ValueError(f"the capture recorder did not start on {self.host}")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    # -- what the child sees, and what it left behind ----------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def env(self) -> dict[str, str]:
        variable = self.BASE_URL_VARS.get(self.provider)
        if variable is None:
            raise ValueError(
                f"--capture does not know which base-URL variable provider {self.provider!r} "
                f"reads; known: {', '.join(sorted(self.BASE_URL_VARS))}"
            )
        return {variable: self.base_url}

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def requests_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Recorded requests whose `captured_at` falls in [start, end].

        Read off disk rather than out of the store, because the recorder owns its store and a
        capture that can be reconstructed from its own files is the same capture a human can
        inspect afterwards. Time-sliced, which is exact only when reps do not overlap — the
        reason `--capture` forces `--workers 1` (upshift.runner).
        """
        out: list[dict[str, Any]] = []
        conversations = self.out_dir / "conversations"
        if not conversations.is_dir():
            return out
        for path in sorted(conversations.glob("*/req_*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            captured = str(record.get("captured_at") or "")
            if start <= captured <= end:
                out.append(record)
        return out
