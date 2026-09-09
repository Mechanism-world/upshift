"""Sandboxed, deterministic executor for shell_gpt's ``execute_shell_command`` tool.

Upstream shell_gpt runs the model's command on the user's own machine with
``subprocess.Popen(cmd, shell=True, stdout=PIPE, stderr=STDOUT)`` and hands the model back
``f"Exit code: {exit_code}, Output:\\n{output.decode()}"`` (see ATTRIBUTION.md). An eval suite
cannot do that: it would be neither safe nor deterministic. So the command runs inside a
throwaway container over a per-episode file tree instead::

    docker run --rm --network none --hostname shellbox --pids-limit 512 \\
        --memory 512m --security-opt no-new-privileges \\
        -v <tmpdir>:/work -w /work -e TZ=UTC -e LC_ALL=C \\
        upshift-shellbox:latest bash -c <command>

(``--hostname`` is pinned because Docker otherwise invents a fresh one per run and
``hostname`` would be nondeterministic.)

Everything else is mirrored: same shell, same combined stdout+stderr stream (the child's
stderr is the same pipe as its stdout, exactly as upstream), same ``Exit code: N, Output:\\n``
envelope. The one shape delta is the dict contract — upshift JSON-encodes whatever
``execute`` returns into the tool message, so upstream's bare string arrives as
``{"output": "Exit code: 0, Output:\\n..."}``.

``initial_state`` schema::

    {"files": {"logs/app-01.log": "line\\nline\\n", "config.json": "{...}"}}

Paths are relative and are materialized under a fresh temp directory. Every file and
directory gets ``FIXED_MTIME`` (2026-01-01 00:00:00 UTC) after writing, so ``ls -l`` inside
the container is byte-identical run to run.

``state()`` is a pure function of the tree's *contents* — never of mtimes, never of the temp
directory's name::

    {"paths":       ["config.json", "logs/app-01.log", ...],          # sorted
     "files":       {relpath: {"sha256": ..., "size": ...}},          # sorted
     "files_text":  {relpath: <content, whitespace-stripped>},        # small text files only
     "tree_sha256": "<digest of the whole files manifest>"}

``files_text`` holds files up to ``TEXT_MANIFEST_MAX_BYTES`` that decode as UTF-8, and its
values are ``.strip()``-ed. That stripping is deliberate: a case that asks the model to write
a count into a file cannot know whether the model will emit ``7`` or ``7\\n``, and both are
correct answers. The ``files``/``tree_sha256`` manifests stay byte-exact, so nothing is lost —
a case picks whichever view matches the fidelity it actually means to assert.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import weakref
from pathlib import Path
from typing import Any

#: Container image built from ``shellbox.Dockerfile``. Override for a differently tagged
#: build; the eval suite's numbers are only comparable across identical images.
IMAGE = os.environ.get("UPSHIFT_SHELLBOX_IMAGE", "upshift-shellbox:latest")

#: Where per-episode trees are materialized. The default lives under ``$HOME`` on purpose:
#: colima/Docker Desktop only bind-mount paths inside the user's home by default, so a
#: ``/tmp`` scratch dir silently mounts empty.
WORKROOT = Path(
    os.environ.get("UPSHIFT_SHELLBOX_WORKROOT", str(Path.home() / ".cache" / "upshift-shellbox"))
)

#: Hard wall-clock cap on one tool call. A command that outlives it is killed and the model
#: is told so. Not configurable: it is part of what the recorded numbers mean.
TIMEOUT_S = 30

#: Every file and directory is stamped with this, so listings are reproducible.
#: 2026-01-01 00:00:00 UTC.
FIXED_MTIME = 1767225600

#: Upper size for a file to appear in the ``files_text`` view of the state.
TEXT_MANIFEST_MAX_BYTES = 200

#: Fixed container hostname — otherwise Docker invents a new one per run and `hostname`
#: would be a source of nondeterminism.
HOSTNAME = "shellbox"

#: Memory ceiling for one command. `--pids-limit` bounds fork bombs; this bounds the other
#: half of the same problem — a command that allocates until the *host* starts swapping.
#: The container's OOM killer stops it instead, and the model sees the resulting exit code
#: like any other failure. Deliberately not swap-limited: `--memory-swap` makes Docker print
#: a kernel-support warning on some hosts, and that warning would land in the model's output
#: and make the transcript host-dependent.
MEMORY_LIMIT = "512m"

_DOCKER = os.environ.get("UPSHIFT_SHELLBOX_DOCKER", "docker")


class Backend:
    """One episode's file tree plus the sandboxed shell over it. Build via
    :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        files = state.get("files") or {}
        if not isinstance(files, dict):
            raise TypeError("initial_state['files'] must be an object of relpath -> content")

        WORKROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ep-", dir=str(WORKROOT)))
        # The tree outlives no one: dropped along with the backend, whenever that happens.
        self._cleanup = weakref.finalize(self, shutil.rmtree, str(self.root), True)

        for relpath, content in sorted(files.items()):
            target = self._resolve_inside(relpath)
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(str(content), encoding="utf-8")
        self._stamp_mtimes()

    # -- public interface ---------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call. Never raises; every failure comes back as an ``error`` dict."""
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            if name != "execute_shell_command":
                return {"error": f"unknown tool: {name}"}
            command = arguments.get("shell_command")
            if command is None or (isinstance(command, str) and not command.strip()):
                return {"error": "missing required argument: shell_command"}
            if not isinstance(command, str):
                return {"error": "invalid argument: shell_command must be a string"}
            return self._run_in_container(command)
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        """Content-only manifest of the tree. See the module docstring for the shape."""
        manifest: dict[str, dict[str, Any]] = {}
        text: dict[str, str] = {}
        for path in sorted(self._regular_files()):
            relpath = path.relative_to(self.root).as_posix()
            data = path.read_bytes()
            manifest[relpath] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            if len(data) <= TEXT_MANIFEST_MAX_BYTES:
                try:
                    text[relpath] = data.decode("utf-8").strip()
                except UnicodeDecodeError:
                    pass
        blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return {
            "paths": list(manifest),
            "files": manifest,
            "files_text": text,
            "tree_sha256": hashlib.sha256(blob).hexdigest(),
        }

    # -- internals ----------------------------------------------------------

    def _resolve_inside(self, relpath: Any) -> Path:
        """Reject anything that would escape the episode's tree (absolute paths, ``..``)."""
        candidate = Path(str(relpath))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"initial_state path must stay inside the tree: {relpath!r}")
        target = (self.root / candidate).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"initial_state path must stay inside the tree: {relpath!r}")
        return target

    def _regular_files(self) -> list[Path]:
        return [p for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()]

    def _stamp_mtimes(self) -> None:
        os.utime(self.root, (FIXED_MTIME, FIXED_MTIME))
        for path in self.root.rglob("*"):
            if path.is_symlink():
                continue
            os.utime(path, (FIXED_MTIME, FIXED_MTIME))

    def _run_in_container(self, command: str) -> dict[str, Any]:
        container = f"upshift-shellbox-{uuid.uuid4().hex[:12]}"
        argv = [
            _DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--name",
            container,
            "--hostname",
            HOSTNAME,
            "--pids-limit",
            "512",
            "--memory",
            MEMORY_LIMIT,
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{self.root}:/work",
            "-w",
            "/work",
            "-e",
            "TZ=UTC",
            "-e",
            "LC_ALL=C",
            IMAGE,
            "bash",
            "-c",
            command,
        ]
        try:
            # stderr shares the stdout pipe, so interleaving matches upstream's
            # Popen(..., stdout=PIPE, stderr=STDOUT) exactly.
            completed = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _force_remove(container)
            return {"error": f"timeout after {TIMEOUT_S}s"}
        except FileNotFoundError:
            return {"error": f"sandbox unavailable: {_DOCKER!r} is not on PATH"}
        except OSError as exc:
            return {"error": f"sandbox unavailable: {exc}"}

        output = completed.stdout.decode("utf-8", errors="replace")
        if completed.returncode == 125:
            # 125 is docker itself refusing to start (missing image, bad flag), never the
            # command's own status. Surfacing it as a shell result would be a lie.
            return {"error": f"sandbox failed to start container: {output.strip()}"}
        return {"output": f"Exit code: {completed.returncode}, Output:\n{output}"}


def _force_remove(container: str) -> None:
    """Best-effort kill of a container whose command outran the timeout."""
    try:
        subprocess.run(
            [_DOCKER, "rm", "-f", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state``."""
    return Backend(initial_state)


def sandbox_available() -> tuple[bool, str]:
    """(ok, reason) — whether the sandbox image can run right now. For tests and preflight."""
    try:
        completed = subprocess.run(
            [_DOCKER, "image", "inspect", IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return False, f"{_DOCKER!r} is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if completed.returncode != 0:
        return False, (
            f"image {IMAGE} is missing; build it with: docker build -t {IMAGE} "
            "-f agents/shell_gpt/shellbox.Dockerfile agents/shell_gpt/"
        )
    return True, ""
