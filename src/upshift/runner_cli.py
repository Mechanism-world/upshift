"""CLI surface for the runner's resume policy. One flag, one place.

`upshift run` and `upshift upgrade` both resume runs from disk, and both need the same
narrowing of what "already done" means, so the argument lives here rather than being written
out twice with two help strings that drift.
"""

from __future__ import annotations

import argparse

RETRY_ERRORED_HELP = (
    "when resuming a run that is already on disk, re-run the reps whose recorded failure was "
    "NOT the model's: a transient provider error (429 / 5xx / timeout, including flex "
    "capacity), a runner error, an exhausted capture continuation. Those reps measured "
    "nothing, so re-running them obtains a result rather than re-rolling one — a recorded 400 "
    "is a measurement and is never re-run. A rep file is replaced only when the new attempt "
    "passes or reaches a new, non-transient outcome; the new record names the error it "
    "replaced in `retried_from`."
)


def add_retry_args(parser: argparse.ArgumentParser) -> None:
    """Add `--retry-errored` to a subcommand that resumes runs (`run`, `upgrade`)."""
    parser.add_argument("--retry-errored", action="store_true", help=RETRY_ERRORED_HELP)


def retry_errored(args: argparse.Namespace) -> bool:
    """Read the flag off parsed args, defaulting to False for a parser that never added it."""
    return bool(getattr(args, "retry_errored", False))
