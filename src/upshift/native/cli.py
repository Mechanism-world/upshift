"""The CLI surface of the native runner: three flags and one authorization gate.

DESIGN.md §C. `add_runner_args(parser)` is called by whoever builds the `run` / `upgrade` /
`verify-patch` parsers; `runner_options(args, ...)` turns the parsed namespace into the
`RunnerOptions` `run_suite` takes. The integration owner calls exactly these two, plus
`describe_runner` when it wants to print the command without running it.

The gate is deliberately loud and deliberately early. `--allow-runner` is the difference
between "upshift reads your agent's files" and "upshift executes your repository", and a user
who has not said the second sentence out loud should never discover it happened. Without the
flag upshift prints the exact argv, the working directory, the isolation mode and the
environment variable names it would set, and exits 2 — the same exit code every other usage
error uses.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from upshift.native.exec import BASE_ENV_VARS, PROVIDER_ENV_VARS, RunnerOptions
from upshift.native.protocol import RunnerSpec
from upshift.schemas import AgentConfig

#: Same code as every other usage error in cli.py.
EXIT_RUNNER_NOT_AUTHORIZED = 2

#: Environment escape hatch for CI, where nobody is at the keyboard to pass a flag.
ALLOW_RUNNER_ENV = "UPSHIFT_ALLOW_RUNNER"


def add_runner_args(parser: argparse.ArgumentParser) -> None:
    """Add `--allow-runner`, `--allow-in-place` and `--capture` to a command's parser."""
    parser.add_argument(
        "--allow-runner",
        action="store_true",
        help="permit upshift to EXECUTE the command in the agent's `runner` block — that is, "
        "to run code from the application's own checkout. Required for any agent.json with a "
        f"`runner` block; {ALLOW_RUNNER_ENV}=1 has the same effect for CI. Without it upshift "
        "prints what it would run and exits 2.",
    )
    parser.add_argument(
        "--allow-in-place",
        action="store_true",
        help='permit `"isolation": "in-place"`, which runs the command directly in your '
        "checkout instead of in a fresh temp copy per rep. Only needed by runners that cannot "
        "be copied (a built container, a huge monorepo); anything the command writes is "
        "written to your working tree and can make rep 2 differ from rep 1.",
    )
    parser.add_argument(
        "--capture",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="record the requests the application itself puts on the wire while the native "
        "runner drives it, and attach them to each rep record as `wire_requests`. Starts the "
        "`upshift capture` recorder on loopback and points the child's provider base URL at "
        "it. Optional DIR is where the capture is written (default: <runs-root>/<run>-wire). "
        "Forces --workers 1, because requests can only be attributed to a rep that ran alone.",
    )


def is_authorized(args: Any) -> bool:
    return bool(getattr(args, "allow_runner", False)) or os.environ.get(ALLOW_RUNNER_ENV) == "1"


def runner_options(args: Any, *, provider: str, capture_env: dict[str, str] | None = None) -> RunnerOptions:
    """`RunnerOptions` for this invocation. Safe by default: unset flags mean "not allowed"."""
    return RunnerOptions(
        allow_runner=is_authorized(args),
        allow_in_place=bool(getattr(args, "allow_in_place", False)),
        capture_env=dict(capture_env or {}),
        provider=provider,
    )


def describe_runner(config: AgentConfig, *, provider: str = "") -> str:
    """Exactly what an execution would do, as text. Printed by the refusal, and by --dry-run."""
    spec = config.runner_spec()
    if spec is None:
        return f"agent {config.name!r} has no `runner` block."
    forwarded = [name for name in BASE_ENV_VARS if name in os.environ]
    forwarded += [name for name in PROVIDER_ENV_VARS.get(provider, ()) if name in os.environ]
    lines = [
        f"agent:      {config.name} ({config.agent_dir})",
        f"command:    {spec.display_command()}",
        f"workdir:    {spec.workdir}  (isolation: {spec.isolation})",
        f"timeout:    {spec.timeout_s:g}s   max output: {spec.max_output_bytes} bytes",
        f"env set:    {', '.join(sorted(spec.env)) or '(none)'}",
        f"env passed: {', '.join(forwarded) or '(none)'}  — nothing else from your environment",
        "stdin:      one upshift result protocol v1 case object per (case, rep)",
    ]
    return "\n".join(lines)


def refusal_text(config: AgentConfig, *, provider: str = "") -> str:
    """The message printed when a runner agent is used without authorization."""
    return (
        "this agent has a `runner` block: running it means EXECUTING the application's own "
        "command, from its own checkout, once per case per rep.\n\n"
        + describe_runner(config, provider=provider)
        + "\n\nNothing was executed. Re-run with --allow-runner (or set "
        f"{ALLOW_RUNNER_ENV}=1) if that is what you want."
    )


def check_authorized(config: AgentConfig, args: Any, *, provider: str = "",
                     printer: Any = None) -> int | None:
    """`None` when execution may proceed, else the exit code the command should return.

    The helper the integration owner calls right after loading the config and before doing any
    work: `code = check_authorized(config, args, provider=args.provider); if code is not None:
    return code`.
    """
    if config.runner is None or is_authorized(args):
        return None
    text = refusal_text(config, provider=provider)
    if printer is not None:
        printer(text)
    else:
        print(text)
    return EXIT_RUNNER_NOT_AUTHORIZED


def spec_or_none(config: AgentConfig) -> RunnerSpec | None:
    """The validated runner block, or None for an ordinary adapter agent."""
    return config.runner_spec()
