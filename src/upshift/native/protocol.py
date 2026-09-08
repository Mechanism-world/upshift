"""upshift result protocol v1 — the contract between upshift and an application's own runner.

DESIGN.md §C. This module holds BOTH directions of the wire and nothing else: what upshift
writes to the child's stdin, what it will accept back on stdout, and how the runner block in
`agent.json` is validated. No subprocess is started here (exec.py) and no episode is assembled
here (runner.py), so the protocol can be read, tested and re-implemented in another language
without reading any other upshift file — which is the point: `examples/runners/node_runner.mjs`
is a reference implementation written against this docstring.

Why the protocol exists at all. 25 of the 54 OpenAI-track rescue cases closed
UNSUPPORTED_FRAMEWORK and the Anthropic track lost 36 the same way: the failing request is
built inside a framework, and reconstructing it as five adapter files is either impossible or
costs a person a day. Those projects almost always ship something that already runs one
scenario end to end — a pytest, an `npm test`, a `python -m app.eval` — and that command builds
the request with the application's own code, at the application's own configuration. The
native runner drives THAT, and upshift keeps only the part it is actually good at: N reps, the
deterministic check engine, the statistics, the diff and the verdict.

Direction 1 — upshift -> child, one JSON object on stdin::

    {"protocol": 1, "case_id": str, "rep": int, "seed": int, "model": str, "endpoint": str,
     "initial_state": {...}, "user_messages": [str, ...], "patch_applied": bool}

Direction 2 — child -> upshift, ONE JSON object as the last line of stdout::

    {"protocol": 1,
     "final_message": str,
     "tool_executions": [{"turn": int, "name": str, "arguments": {}, "result": {},
                          "segment": int (optional, default 0)}],
     "final_state": {},
     "api_calls": [{"request": {}, "response": {}|null, "error": {}|null}]   (optional)
     "api_error": null | {"message": str, "status_code": int|null, "type": str},
     "usage": {"input_tokens": int, "output_tokens": int}                    (optional)
    }

Everything else the command prints must go to stderr; it is captured (bounded) and recorded as
diagnostics. Only the LAST non-empty stdout line is parsed, so a runner that logs to stdout by
accident still works as long as its result is printed last — but a runner that prints nothing
parseable is a `runner_error`, never a behavioural failure.

The failure classes are the load-bearing part. A non-zero exit, a timeout, malformed JSON, a
missing/incompatible `protocol` and a truncated stdout are all *upshift's harness failing to
observe the application*, not the model behaving differently. Recording them as behavioural
failures would manufacture regressions out of a flaky test command; they are recorded as an
`api_error` whose type is `runner_error` instead, which the differ names and the verdict treats
as INCONCLUSIVE-worthy (DESIGN.md §D) rather than as evidence about a model.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The only protocol version upshift speaks. Bumping it is a DESIGN.md change.
PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Non-behavioural failure classes (DESIGN.md §C/§D/§F)
# ---------------------------------------------------------------------------

#: The harness could not observe the application: bad exit, timeout, unparseable or truncated
#: output, wrong protocol version. Recorded as `api_error.error_type` (and mirrored into the
#: `type` key every provider error already uses, so existing readers see it too).
RUNNER_ERROR = "runner_error"

#: A capture-derived episode needed more assistant turns than the recording provided and
#: `agent.json` `continuation` is "fail" (DESIGN.md §F). Same class as `runner_error`: the
#: evidence ran out, the model did not misbehave.
CONTINUATION_EXHAUSTED = "continuation_exhausted"

#: The provider could not answer for a reason that is about its capacity, not about the model:
#: 429 (including flex's "Flex does not have sufficient resources"), 5xx, a timeout, a dropped
#: connection. `runner.py` retries these per rep with a bounded backoff and records this type
#: only when the retries are exhausted. Non-behavioural for the same reason as the two above:
#: nothing about the model was measured, and counting it as a failing rep moves the pass rate
#: the verdict is computed from (rescue-ops ghi56-019, ghi56-006, ghi56-021).
TRANSIENT_PROVIDER_ERROR = "transient_provider_error"

#: Every error type in this tuple is NON-BEHAVIOURAL. differ.py may map any of them to one
#: signature; verdict.py must never let one produce SAFE or SAFE WITH PATCH.
NON_BEHAVIOURAL_ERROR_TYPES = (RUNNER_ERROR, CONTINUATION_EXHAUSTED, TRANSIENT_PROVIDER_ERROR)

#: `episode_source` values recorded on every rep (DESIGN.md §A companion field).
SOURCE_RECORDED_PLAYBACK = "recorded_playback"
SOURCE_LIVE_MODEL = "live_model"
SOURCE_NATIVE_APPLICATION = "native_application"
EPISODE_SOURCES = (SOURCE_RECORDED_PLAYBACK, SOURCE_LIVE_MODEL, SOURCE_NATIVE_APPLICATION)


def error_payload(message: str, *, error_type: str = RUNNER_ERROR) -> dict[str, Any]:
    """The `api_error` dict for a non-behavioural failure.

    `type` and `error_type` carry the same value on purpose: `type` is the key every provider
    error already writes (providers/base.ProviderAPIError.to_dict) and every existing reader
    looks at, `error_type` is the name DESIGN.md §C gives it. One value, two spellings, no
    reader that has to know which.
    """
    if error_type not in NON_BEHAVIOURAL_ERROR_TYPES:
        raise ValueError(f"{error_type!r} is not a non-behavioural error type")
    return {
        "status_code": None,
        "message": message,
        "type": error_type,
        "error_type": error_type,
    }


def is_non_behavioural(api_error: Any) -> bool:
    """True when a recorded api_error is a harness failure rather than a model behaviour."""
    if not isinstance(api_error, dict):
        return False
    seen = api_error.get("error_type") or api_error.get("type")
    return seen in NON_BEHAVIOURAL_ERROR_TYPES


class ProtocolError(ValueError):
    """The child's stdout was not a protocol v1 result. Always becomes a `runner_error`."""


# ---------------------------------------------------------------------------
# The `runner` block in agent.json
# ---------------------------------------------------------------------------

KIND_COMMAND = "command"
RUNNER_KINDS = (KIND_COMMAND,)

ISOLATION_WORKDIR_COPY = "workdir-copy"
ISOLATION_IN_PLACE = "in-place"
ISOLATIONS = (ISOLATION_WORKDIR_COPY, ISOLATION_IN_PLACE)

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024

#: Template variables allowed in `runner.env` values.
ENV_TEMPLATE_KEYS = ("model", "endpoint", "case_id", "rep", "seed")


@dataclass(frozen=True)
class RunnerSpec:
    """A validated `agent.json` `runner` block.

    `command` is an argv LIST and never a shell string — there is no shell anywhere in the
    native path, so a repository whose name contains a quote or a `;` cannot become an
    injection. That is the same rule the `adapt` git-clone hardening landed on
    (SECURITY.md, 2026-09-03 pre-launch security pass).
    """

    kind: str
    workdir: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    isolation: str = ISOLATION_WORKDIR_COPY

    @staticmethod
    def from_dict(raw: Any, where: str = "agent.json") -> RunnerSpec:
        """Parse and validate. Every failure is a ValueError, as everywhere in upshift."""
        if not isinstance(raw, dict):
            raise ValueError(  # noqa: TRY004 - authoring errors are ValueError throughout
                f"{where}: `runner` must be an object (got {type(raw).__name__}); see ADAPTER.md"
            )
        kind = str(raw.get("kind") or KIND_COMMAND)
        if kind not in RUNNER_KINDS:
            raise ValueError(f"{where}: unknown runner kind {kind!r} (expected one of {RUNNER_KINDS})")

        command = raw.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) for part in command
        ):
            raise ValueError(
                f"{where}: `runner.command` must be a non-empty list of strings — an argv list, "
                f"never a shell string (upshift never runs a shell). Write "
                f'["python", "-m", "myapp.eval_case"], not "python -m myapp.eval_case".'
            )

        workdir = raw.get("workdir", ".")
        if not isinstance(workdir, str):
            raise ValueError(  # noqa: TRY004
                f"{where}: `runner.workdir` must be a string relative to the agent dir")

        env_raw = raw.get("env") or {}
        if not isinstance(env_raw, dict):
            raise ValueError(  # noqa: TRY004
                f"{where}: `runner.env` must be an object of NAME -> value strings")
        env: dict[str, str] = {}
        for name, value in env_raw.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"{where}: `runner.env` has a non-string variable name"
                )
            if not isinstance(value, str | int | float | bool):
                raise ValueError(  # noqa: TRY004
                    f"{where}: `runner.env[{name}]` must be a string (got {type(value).__name__})"
                )
            env[name] = str(value)

        timeout_s = _positive_number(raw.get("timeout_s", DEFAULT_TIMEOUT_S), f"{where}: runner.timeout_s")
        max_output_bytes = int(
            _positive_number(raw.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES),
                             f"{where}: runner.max_output_bytes")
        )
        isolation = str(raw.get("isolation") or ISOLATION_WORKDIR_COPY)
        if isolation not in ISOLATIONS:
            raise ValueError(
                f"{where}: unknown runner isolation {isolation!r} (expected one of {ISOLATIONS})"
            )
        # Validate templates up front: a typo'd {mdoel} should fail before a run spends time.
        for name, value in env.items():
            render_template(value, {key: "" for key in ENV_TEMPLATE_KEYS},
                            where=f"{where}: runner.env[{name}]")
        return RunnerSpec(
            kind=kind,
            workdir=workdir,
            command=tuple(command),
            env=env,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
            isolation=isolation,
        )

    def resolved_workdir(self, agent_dir: str | Path) -> Path:
        """The checkout this runner executes in — always inside the agent dir's tree.

        `workdir` is relative to the agent directory and may not escape it: an agent.json is
        an input like any other, and `"workdir": "../../.."` would otherwise let one copy the
        operator's home directory into a temp dir on the way to running a command in it.
        """
        agent_dir = Path(agent_dir).resolve()
        candidate = (agent_dir / self.workdir).resolve()
        if candidate != agent_dir and agent_dir not in candidate.parents:
            raise ValueError(
                f"runner.workdir {self.workdir!r} resolves outside the agent directory "
                f"({candidate}); it must name the application checkout inside {agent_dir}"
            )
        if not candidate.is_dir():
            raise ValueError(f"runner.workdir {self.workdir!r} is not a directory ({candidate})")
        return candidate

    def display_command(self) -> str:
        return shlex.join(self.command)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "workdir": self.workdir,
            "command": list(self.command),
            "env": dict(self.env),
            "timeout_s": self.timeout_s,
            "max_output_bytes": self.max_output_bytes,
            "isolation": self.isolation,
        }


def _positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(  # noqa: TRY004
            f"{where} must be a positive number (got {value!r})"
        )
    if value <= 0:
        raise ValueError(f"{where} must be positive (got {value!r})")
    return float(value)


def render_template(text: str, values: dict[str, Any], *, where: str) -> str:
    """`{model}`-style substitution over the fixed key set, with a readable failure.

    `str.format` would also honour attribute access and indexing (`{model.__class__}`), so the
    substitution is done by hand over a closed key list.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "{":
            end = text.find("}", index)
            if end == -1:
                raise ValueError(f"{where}: unclosed '{{' in template {text!r}")
            key = text[index + 1 : end]
            if key not in values:
                raise ValueError(
                    f"{where}: unknown template variable {{{key}}} — allowed: "
                    f"{', '.join('{' + k + '}' for k in ENV_TEMPLATE_KEYS)}"
                )
            out.append(str(values[key]))
            index = end + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Direction 1: the case, on stdin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseRequest:
    case_id: str
    rep: int
    seed: int
    model: str
    endpoint: str
    initial_state: dict[str, Any]
    user_messages: list[str]
    patch_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "case_id": self.case_id,
            "rep": self.rep,
            "seed": self.seed,
            "model": self.model,
            "endpoint": self.endpoint,
            "initial_state": self.initial_state,
            "user_messages": list(self.user_messages),
            "patch_applied": self.patch_applied,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def template_values(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "endpoint": self.endpoint,
            "case_id": self.case_id,
            "rep": self.rep,
            "seed": self.seed,
        }


# ---------------------------------------------------------------------------
# Direction 2: the result, on stdout
# ---------------------------------------------------------------------------


@dataclass
class RunnerResult:
    final_message: str = ""
    tool_executions: list[dict[str, Any]] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    api_error: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)


def parse_result(stdout: str) -> RunnerResult:
    """The LAST JSON object on stdout, validated. Raises ProtocolError with a usable reason.

    Anything a runner prints before the result (a test framework's own output, a progress bar)
    is tolerated, because telling a developer "your existing command must print nothing" would
    defeat the entire point of running their existing command.
    """
    line = _last_json_line(stdout)
    if line is None:
        raise ProtocolError(
            "the runner printed no JSON object on stdout; upshift result protocol v1 requires "
            "one JSON object as the last line of stdout (everything else goes to stderr)"
        )
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"the runner's last stdout line is not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(
            f"the runner's result must be a JSON object (got {type(payload).__name__})"
        )
    version = payload.get("protocol")
    if version is None:
        raise ProtocolError(
            "the runner's result has no `protocol` key; upshift refuses to interpret output "
            "that does not declare the protocol it speaks"
        )
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"the runner speaks protocol {version!r}, upshift speaks {PROTOCOL_VERSION}"
        )

    api_error = payload.get("api_error")
    if api_error is not None and not isinstance(api_error, dict):
        raise ProtocolError(
            f"`api_error` must be null or an object (got {type(api_error).__name__})"
        )
    return RunnerResult(
        final_message=str(payload.get("final_message") or ""),
        tool_executions=_tool_executions(payload.get("tool_executions")),
        final_state=_object(payload.get("final_state"), "final_state"),
        api_calls=_api_calls(payload.get("api_calls")),
        api_error=dict(api_error) if api_error else None,
        usage=_usage(payload.get("usage")),
    )


def _last_json_line(stdout: str) -> str | None:
    for raw in reversed((stdout or "").splitlines()):
        line = raw.strip()
        if line.startswith("{") and line.endswith("}"):
            return line
    return None


def _object(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError(f"`{what}` must be an object (got {type(value).__name__})")
    return value


def _tool_executions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolError(f"`tool_executions` must be a list (got {type(value).__name__})")
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ProtocolError(f"`tool_executions[{index}]` must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(f"`tool_executions[{index}].name` must be a non-empty string")
        out.append(
            {
                "turn": _int(entry.get("turn", index), f"tool_executions[{index}].turn"),
                "segment": _int(entry.get("segment", 0), f"tool_executions[{index}].segment"),
                "name": name,
                "arguments": _object(entry.get("arguments"), f"tool_executions[{index}].arguments"),
                "result": _object(entry.get("result"), f"tool_executions[{index}].result"),
            }
        )
    return out


def _api_calls(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolError(f"`api_calls` must be a list (got {type(value).__name__})")
    out = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ProtocolError(f"`api_calls[{index}]` must be an object")
        out.append(
            {
                "request": _object(entry.get("request"), f"api_calls[{index}].request"),
                "response": entry.get("response") if isinstance(entry.get("response"), dict) else None,
                "error": entry.get("error") if isinstance(entry.get("error"), dict) else None,
            }
        )
    return out


def _usage(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError(f"`usage` must be an object (got {type(value).__name__})")
    out: dict[str, int] = {}
    for key, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int):
            continue  # a runner that reports a float or a string token count reports nothing
        out[str(key)] = count
    return out


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"`{what}` must be an integer (got {value!r})")
    return value
