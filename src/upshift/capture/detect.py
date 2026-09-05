"""Which framework sent this request, read off the headers it chose to send.

Detection is a convenience, never a load-bearing fact: it selects a row of
`docs/framework-mapping.md` for the report, and `upshift capture --framework <name>` overrides
it. Every rule below matches a literal token that the sender itself put in a header, and the
observed `user-agent` / `x-stainless-*` values are recorded verbatim in `index.json`, so a
detection can always be checked against the bytes rather than trusted.

Ordering matters: a framework that wraps an Anthropic SDK still sends that SDK's
`x-stainless-*` headers, so the specific names are matched before the generic SDK fallback.
"""

from __future__ import annotations

from typing import Any

UNKNOWN = "unknown"

#: Headers worth keeping for detection and for the report. Everything else is dropped from
#: the recorded header map except the small allowlist in `record.py`.
DETECTION_HEADERS = ("user-agent", "x-app", "x-stainless-lang", "x-stainless-package-version",
                     "x-stainless-runtime", "x-stainless-runtime-version", "x-stainless-os",
                     "x-stainless-retry-count", "x-stainless-helper-method")

#: (framework name, header, lowercase literal the sender puts there). First match wins.
RULES: tuple[tuple[str, str, str], ...] = (
    ("opencode", "user-agent", "opencode"),
    ("litellm", "user-agent", "litellm"),
    ("langchain-anthropic", "user-agent", "langchain"),
    ("pydantic-ai", "user-agent", "pydantic-ai"),
    ("claude-agent-sdk", "user-agent", "claude-cli"),
    ("claude-agent-sdk", "user-agent", "claude-agent-sdk"),
    ("claude-agent-sdk", "x-app", "cli"),
    ("vercel-ai-sdk", "user-agent", "ai-sdk"),
    ("vercel-ai-sdk", "x-stainless-package-version", "ai-sdk"),
    # Generic fallbacks: the Anthropic SDKs' own signature, used directly.
    ("anthropic-sdk-python", "x-stainless-lang", "python"),
    ("anthropic-sdk-typescript", "x-stainless-lang", "js"),
)


def detect(headers: dict[str, str]) -> tuple[str, str]:
    """(framework name, how it was decided). `("unknown", "")` when no rule matches."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    for name, header, token in RULES:
        value = lowered.get(header, "")
        if token in value.lower():
            return name, f"{header}: {value!r} contains {token!r}"
    return UNKNOWN, ""


def detection_fields(headers: dict[str, str]) -> dict[str, str]:
    """The subset of headers detection looked at, for the record."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return {key: lowered[key] for key in DETECTION_HEADERS if key in lowered}


def summarize(records: list[dict[str, Any]], override: str | None) -> dict[str, Any]:
    """Whole-capture detection summary for index.json.

    Reports every framework any request looked like — a gateway in front of two clients is a
    real shape and must not be flattened to one name — plus the override, if one was given.
    """
    votes: dict[str, str] = {}
    agents: list[str] = []
    for record in records:
        headers = record.get("headers") or {}
        name, how = detect(headers)
        if name != UNKNOWN and name not in votes:
            votes[name] = how
        agent = headers.get("user-agent")
        if isinstance(agent, str) and agent and agent not in agents:
            agents.append(agent)
    detected = sorted(votes)
    return {
        "override": override,
        "detected": detected,
        "framework": override or (detected[0] if len(detected) == 1 else UNKNOWN),
        "evidence": votes,
        "user_agents": agents,
    }
