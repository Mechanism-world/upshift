"""Running someone else's command, safely enough to run it 5 times a case.

DESIGN.md §C. Everything in this module exists because the native runner is upshift's first
execution path that runs code upshift did not write and did not read: the developer's own test
or eval command, in their own checkout. The pre-launch security pass (CLAUDE.md, 2026-09-03)
set the house rules for generated and third-party code; this module applies them to a process:

* **Never without authorization.** `RunnerOptions.allow_runner` is False by default and nothing
  here starts a process without it. The CLI gate (`--allow-runner` / `UPSHIFT_ALLOW_RUNNER=1`)
  is a second, earlier check — this one is the one that cannot be forgotten by a caller.
* **argv, never a shell.** `subprocess.Popen(list, shell=False)`. There is no code path in
  upshift that turns a runner command into a shell string.
* **A minimal environment.** The child gets PATH/HOME/LANG/TMPDIR, the runner's own `env`, and
  the provider key variables this run needs — not the operator's environment. A test command
  that inherited everything would carry unrelated production credentials into a process upshift
  runs 5 times per case, and a capture of it would carry them onto disk.
* **A copy, not the checkout.** Default isolation copies `workdir` into a temp directory per
  rep and deletes it afterwards, so a command that writes files (most eval harnesses do) cannot
  make rep 2 different from rep 1 — the determinism ADAPTER.md requires, enforced instead of
  requested. Symlinks are copied AS symlinks, never followed, for the reason the `adapt` repo
  walk learned the hard way: a checked-in symlink to `~/.ssh/id_rsa` otherwise becomes a file
  in the copy.
* **Bounded output and a hard deadline.** stdout and stderr are read by threads that stop
  accumulating at `max_output_bytes` (and keep draining, so the child never blocks on a full
  pipe), and the whole process GROUP is killed on timeout or SIGINT — killing only the parent
  leaves `npm test`'s node processes running.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from upshift.native.protocol import ISOLATION_IN_PLACE, RunnerSpec

#: Environment variables the child always gets, when the parent has them. Deliberately short:
#: everything else must be named in `runner.env` or be a provider key below.
BASE_ENV_VARS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")

#: Provider credentials/config forwarded when the run needs that provider. A native runner
#: calls the API itself, so it needs the key — but it gets only the one its provider uses.
PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT", "OPENAI_BASE_URL"),
    "openai-batch": ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT", "OPENAI_BASE_URL"),
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_WORKSPACE_ID",
        "ANTHROPIC_BASE_URL",
    ),
}

#: A runner whose isolation is workdir-copy never needs these; they exist so a copy of a big
#: checkout does not also copy build detritus that makes every rep slow.
COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv",
    "node_modules", ".git",
)


class RunnerNotAuthorized(ValueError):
    """A runner agent was executed without `--allow-runner` (or `--allow-in-place`)."""


@dataclass
class RunnerOptions:
    """What the CLI decided about executing this repository's code.

    Defaults are the safe ones: nothing runs, nothing runs in place, nothing is captured. A
    caller that forgets to pass options gets a refusal, not an execution.
    """

    allow_runner: bool = False
    allow_in_place: bool = False
    #: Base URLs handed to the child so its own SDK talks to the loopback capture recorder.
    capture_env: dict[str, str] = field(default_factory=dict)
    #: Provider name whose key variables the child may see.
    provider: str = ""
    #: Extra environment variables (after templating) merged last. Used by tests and by
    #: `verify-patch` to mark a patched run.
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    truncated: bool
    duration_s: float
    workdir: str
    command: list[str]


def build_env(
    spec: RunnerSpec,
    options: RunnerOptions,
    template_values: dict[str, Any],
    *,
    parent_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The child's complete environment. Nothing that is not listed here reaches it."""
    from upshift.native.protocol import render_template

    parent = os.environ if parent_env is None else parent_env
    env: dict[str, str] = {name: parent[name] for name in BASE_ENV_VARS if name in parent}
    for name in PROVIDER_ENV_VARS.get(options.provider, ()):  # only this provider's variables
        if name in parent:
            env[name] = parent[name]
    for name, value in spec.env.items():
        env[name] = render_template(value, template_values, where=f"runner.env[{name}]")
    # The capture recorder's base URL wins over anything the runner block declared: pointing
    # the child somewhere else would make `--capture` silently record nothing.
    env.update(options.capture_env)
    env.update(options.extra_env)
    env.setdefault("UPSHIFT_NATIVE_RUNNER", "1")
    return env


@contextlib.contextmanager
def prepared_workdir(spec: RunnerSpec, agent_dir: str | Path, options: RunnerOptions):
    """Yield the directory the command runs in, and clean it up afterwards.

    `workdir-copy` (the default) yields a fresh temp copy that is deleted on exit; `in-place`
    yields the real checkout and requires `--allow-in-place`, because a command that writes
    into the developer's tree is exactly what the copy exists to prevent.
    """
    source = spec.resolved_workdir(agent_dir)
    if spec.isolation == ISOLATION_IN_PLACE:
        if not options.allow_in_place:
            raise RunnerNotAuthorized(
                f'runner.isolation is "{ISOLATION_IN_PLACE}", which runs the command directly in '
                f"{source} and lets it modify your checkout. Pass --allow-in-place to permit it, "
                f'or use "workdir-copy" (the default), which copies the checkout per rep.'
            )
        yield source
        return
    parent = tempfile.mkdtemp(prefix="upshift-runner-")
    target = Path(parent) / source.name
    try:
        shutil.copytree(source, target, symlinks=True, ignore=COPY_IGNORE)
        yield target
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def run_command(
    spec: RunnerSpec,
    agent_dir: str | Path,
    stdin_payload: str,
    options: RunnerOptions,
    template_values: dict[str, Any],
) -> ExecOutcome:
    """Run the runner command once. Never raises for the child's behaviour, only for ours."""
    if not options.allow_runner:
        raise RunnerNotAuthorized(
            "this agent has a `runner` block, which means running code from the application's "
            "own checkout. upshift will not start it without explicit authorization: pass "
            "--allow-runner (or set UPSHIFT_ALLOW_RUNNER=1). Command it would run: "
            f"{spec.display_command()}"
        )
    env = build_env(spec, options, template_values)
    started = time.monotonic()
    with prepared_workdir(spec, agent_dir, options) as workdir:
        popen_kwargs: dict[str, Any] = {
            "cwd": str(workdir),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "close_fds": True,
        }
        if os.name == "posix":
            # Its own process group, so a timeout or a Ctrl-C kills the whole tree rather than
            # a shell-less parent whose children keep the API key and keep spending.
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(list(spec.command), **popen_kwargs)
        except OSError as exc:
            return ExecOutcome(
                stdout="",
                stderr=f"could not start {spec.display_command()}: {exc}",
                exit_code=None,
                timed_out=False,
                truncated=False,
                duration_s=round(time.monotonic() - started, 3),
                workdir=str(workdir),
                command=list(spec.command),
            )
        stdout_reader = _BoundedReader(process.stdout, spec.max_output_bytes)
        stderr_reader = _BoundedReader(process.stderr, spec.max_output_bytes)
        timed_out = False
        try:
            _write_stdin(process, stdin_payload)
            try:
                process.wait(timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
                process.wait(timeout=10)
        except KeyboardInterrupt:
            _kill_group(process)
            raise
        finally:
            stdout_reader.join()
            stderr_reader.join()
        return ExecOutcome(
            stdout=stdout_reader.text(),
            stderr=stderr_reader.text(),
            exit_code=process.returncode,
            timed_out=timed_out,
            truncated=stdout_reader.truncated or stderr_reader.truncated,
            duration_s=round(time.monotonic() - started, 3),
            workdir=str(workdir),
            command=list(spec.command),
        )


def _write_stdin(process: subprocess.Popen, payload: str) -> None:
    stream = process.stdin
    if stream is None:
        return
    try:
        stream.write(payload.encode("utf-8"))
        stream.flush()
    except (BrokenPipeError, OSError):
        # A runner that ignores stdin and exits is a protocol failure, reported by the parse
        # step with the command's own stderr attached — not a crash here.
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _kill_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (or the child, where groups do not exist)."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()


class _BoundedReader:
    """Drains a pipe on its own thread, keeping at most `cap` bytes.

    Draining past the cap matters: a child whose stdout pipe fills blocks forever, and a
    "bounded" reader that stopped reading would turn an over-chatty test command into a hang
    that only the timeout ends.
    """

    def __init__(self, stream: Any, cap: int) -> None:
        self._stream = stream
        self._cap = cap
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        if stream is not None:
            self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(65536)
                if not chunk:
                    break
                room = self._cap - self._size
                if room > 0:
                    self._chunks.append(chunk[:room])
                    self._size += min(room, len(chunk))
                if len(chunk) > room:
                    self.truncated = True
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                self._stream.close()

    def join(self, timeout: float = 10.0) -> None:
        if self._stream is not None:
            self._thread.join(timeout=timeout)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")
