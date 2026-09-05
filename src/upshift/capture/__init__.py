"""`upshift capture` — record a framework's real `/v1/messages` requests at the wire.

Why this exists: 36 of the 52 Anthropic rescue cases closed `UNSUPPORTED_FRAMEWORK` because
the failing request is built inside opencode, litellm, pydantic-ai, langchain, the Claude
Agent SDK or a gateway, not inside anything expressible as the five adapter files
(rescue-ops `summaries/EXEC_SUMMARY.md` (e)/(f), `LAB_BATCH_1.md` §2).

The principle, and the whole reason this is small: **upshift never reads a framework's
source.** It stands between the framework and `api.anthropic.com`, records the bytes the
framework actually sends, and adapts those verbatim. Everything downstream — runner, differ,
repair loop, patch, verdict — is unchanged, because a capture-derived agent directory is an
ordinary agent directory.
"""

from __future__ import annotations

__all__ = ["adapt_from_capture", "run_capture"]


def __getattr__(name: str):  # lazy: `upshift capture` must not import the world at CLI start
    if name == "run_capture":
        from upshift.capture.server import run_capture

        return run_capture
    if name == "adapt_from_capture":
        from upshift.capture.adapt import adapt_from_capture

        return adapt_from_capture
    raise AttributeError(name)
