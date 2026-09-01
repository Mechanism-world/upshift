"""`upshift adapt` — generate an agent directory from an agent codebase (DESIGN.md v0.2).

Pipeline: inventory (walk + rank + call sites) -> extract (model over bounded evidence) ->
verify (mechanical verbatim-citation gate) -> generate (the five files) -> report.

The load-bearing rule: confidence is derived from the verification gate, never self-reported
by the model. Undetermined-with-a-pointer beats guessed.
"""

from __future__ import annotations


class AdaptAborted(Exception):
    """Raised when a budget (cost) stops the pipeline; the caller still writes a report."""

    def __init__(self, message: str, stage: str = "extract") -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage


__all__ = ["AdaptAborted"]
