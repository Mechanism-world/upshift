"""What the recorder saw that the adapter cannot carry — named, counted, and machine-readable.

`capture/adapt.py` already reports the deviations it MAKES. This module reports the ones it
cannot avoid: fields that were on the wire and have no slot in an upshift agent directory. The
difference matters because a silent omission reads, downstream, as an absence: a run whose
`response_format` was dropped measures an agent that never had one, and the report says nothing
about it. That failure is not hypothetical — `upshift adapt` did exactly this, for money:
"`response_format` is in `BLOCKED_PARAMS` … adapt silently deletes the entire subject of a
structured-output failure" (rescue-ops `ops/cases/ghisdk-051/CASE.md:308`, $0.3060 spent).

Each finding is a dict with a stable shape, written to `unsupported_fields.json` in the
generated agent directory and rendered as a table in `ADAPT_EDITS.md`::

    {"field": "response_format", "where": "request", "kind": "dropped_param",
     "count": 4, "detail": "...", "sample": "...", "impact": "..."}

`kind` is the machine-readable classification; the vocabulary is closed (KINDS below) so a
report or a queue can group on it. `count` is how many recorded requests/responses carried it.

The catalogue below is derived from what the two rescue tracks actually hit, and each entry
cites the case it came from. It is deliberately a denylist-free design: anything on a recorded
request that upshift does not carry is reported, so the NEXT field a provider adds shows up as
a finding on its first capture rather than as silence.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

#: Closed vocabulary for `kind`.
KIND_DROPPED_PARAM = "dropped_param"
KIND_TOOL_FIELD = "tool_field"
KIND_SERVER_TOOL = "server_tool"
KIND_STREAMING = "streaming"
KIND_BETA_HEADER = "beta_header"
KIND_RESPONSE_BLOCK = "response_block"
KIND_ERROR_RESPONSE = "error_response"
KIND_GATEWAY = "gateway"
KIND_UNRECORDED_PATH = "unrecorded_path"
KINDS = (
    KIND_DROPPED_PARAM, KIND_TOOL_FIELD, KIND_SERVER_TOOL, KIND_STREAMING, KIND_BETA_HEADER,
    KIND_RESPONSE_BLOCK, KIND_ERROR_RESPONSE, KIND_GATEWAY, KIND_UNRECORDED_PATH,
)

#: Request fields the adapter DOES carry, each into a named place. Everything else on a
#: recorded body is a finding. `stream` is carried in the sense that the recorder reassembles
#: it, but the streaming BOUNDARIES are not replayed, so it gets its own finding below.
CARRIED_REQUEST_FIELDS = frozenset(
    {
        "model",           # agent.json model
        "messages",        # cases/cases.json (user turns) + the replay backend
        "system",          # system_prompt.txt
        "tools",           # tools.json
        "max_tokens",      # agent.json params
        "temperature",
        "top_p",
        "top_k",
        "tool_choice",     # agent.json params or turn_params
        "thinking",
        "output_config",
        "service_tier",
        "stream",          # reassembled; see the streaming finding
    }
)

#: Response content block types the adapter can represent in a case or a replayed result.
#: `thinking`/`redacted_thinking` are dropped BY DESIGN (replaying a signature is what causes
#: the invalidation 400, ADAPT_EDITS structural deviation 1), so they are not findings here.
REPRESENTED_BLOCK_TYPES = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})

#: Per-field notes for the fields upshift has actually been bitten by. Anything not listed
#: still becomes a finding, with a generic impact line — the catalogue improves the message,
#: it is never what decides whether something is reported.
_KNOWN_REQUEST_FIELDS: dict[str, tuple[str, str]] = {
    "response_format": (
        KIND_DROPPED_PARAM,
        (
            "structured-output format. upshift has no slot for it and the replayed request will not "
            "ask for structured output at all — so a structured-output regression becomes invisible "
            "(rescue-ops ops/cases/ghisdk-051/CASE.md:308)."
        ),
    ),
    "metadata": (
        KIND_DROPPED_PARAM,
        (
            "request metadata (user ids, trace ids). Not replayed; a behaviour that depends on it "
            "will not reproduce."
        ),
    ),
    "mcp_servers": (
        KIND_SERVER_TOOL,
        (
            "server-side MCP connectors. The tools they expose are executed by the API, not by any "
            "backend upshift can write (rescue-ops A-097)."
        ),
    ),
    "container": (
        KIND_SERVER_TOOL,
        (
            "server-side code execution container. Not expressible as an adapter tool "
            "(rescue-ops A-097)."
        ),
    ),
    "betas": (
        KIND_BETA_HEADER,
        (
            "beta features requested in the body. upshift sends no beta flags, so a behaviour gated "
            "on one will not reproduce (rescue-ops A-096: `thinking.display`)."
        ),
    ),
    "previous_response_id": (
        KIND_DROPPED_PARAM,
        (
            "server-side conversation state. A one-shot replay has no prior response to link to; "
            "DESIGN.md §G drops state-linking params and records the drop."
        ),
    ),
    "store": (KIND_DROPPED_PARAM, "server-side storage flag; not replayed."),
    "conversation": (KIND_DROPPED_PARAM, "server-side conversation id; not replayed."),
    "context_management": (
        KIND_DROPPED_PARAM,
        "server-side context editing. The replayed episode manages its own history.",
    ),
}


_GENERIC_IMPACT = (
    "recorded on the wire and not written to the agent directory: the replayed request does "
    "not carry it, so any behaviour that depends on it will not reproduce"
)


def audit(
    index: dict[str, Any],
    conversations: list[dict[str, Any]],
    *,
    tools_dropped: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every field the recording holds and the adapter cannot carry, as findings.

    `tools_dropped` is what `capture/adapt._tools_chat_style` already discovered per tool; it
    is folded in here so the machine-readable list is complete rather than split between two
    reports.
    """
    findings: list[dict[str, Any]] = []
    request_fields: Counter = Counter()
    samples: dict[str, Any] = {}
    beta_headers: Counter = Counter()
    block_types: Counter = Counter()
    error_statuses: Counter = Counter()
    streaming = 0
    requests = 0

    for conversation in conversations:
        for turn in conversation.get("turns") or []:
            request = turn.get("request") or {}
            body = request.get("body")
            if isinstance(body, dict):
                requests += 1
                for key, value in body.items():
                    if key in CARRIED_REQUEST_FIELDS:
                        continue
                    request_fields[key] += 1
                    samples.setdefault(key, value)
                if body.get("stream"):
                    streaming += 1
            headers = request.get("headers") or {}
            beta = headers.get("anthropic-beta")
            if beta:
                beta_headers[str(beta)] += 1
            response = turn.get("response") or {}
            status = response.get("status")
            if isinstance(status, int) and status >= 400:
                error_statuses[status] += 1
            for block in ((response.get("body") or {}).get("content") or []):
                if isinstance(block, dict):
                    kind = str(block.get("type") or "")
                    if kind and kind not in REPRESENTED_BLOCK_TYPES:
                        block_types[kind] += 1

    for field, count in sorted(request_fields.items()):
        kind, detail = _KNOWN_REQUEST_FIELDS.get(field, (KIND_DROPPED_PARAM, _GENERIC_IMPACT))
        findings.append(
            {
                "field": field,
                "where": "request",
                "kind": kind,
                "count": count,
                "detail": detail,
                "sample": _sample(samples.get(field)),
            }
        )

    for tool in tools_dropped or []:
        findings.append(
            {
                "field": f"tools[{tool['name']}].{tool['field']}",
                "where": "request.tools",
                "kind": KIND_SERVER_TOOL if tool.get("server_tool") else KIND_TOOL_FIELD,
                "count": 1,
                "detail": (
                    "the chat-style tool shape ADAPTER.md requires carries only name, "
                    "description and input_schema. A dropped `type` turns a server tool into "
                    "an ordinary custom tool with an empty schema — a different agent from the "
                    "captured one (rescue-ops A-075 §6.3(b))."
                ),
                "sample": _sample(tool.get("value")),
            }
        )

    if streaming:
        findings.append(
            {
                "field": "stream",
                "where": "request",
                "kind": KIND_STREAMING,
                "count": streaming,
                "detail": (
                    "the framework streamed. The recorder reassembles the SSE events into the "
                    "message they add up to and the adapter replays that message in one piece, "
                    "so CONTENT is preserved and BOUNDARIES are not: nothing downstream can see "
                    "where a chunk ended, when the first token arrived, or how a partial tool "
                    "call was assembled. A defect that lives in the event stream (rescue-ops "
                    "A-061, A-062) does not survive the replay."
                ),
                "sample": "true",
            }
        )

    for value, count in sorted(beta_headers.items()):
        findings.append(
            {
                "field": "anthropic-beta",
                "where": "request.headers",
                "kind": KIND_BETA_HEADER,
                "count": count,
                "detail": (
                    "the framework requested beta features by header. upshift sends no beta "
                    "headers, so anything gated on one behaves differently in the replay "
                    "(rescue-ops A-096)."
                ),
                "sample": value,
            }
        )

    for block_type, count in sorted(block_types.items()):
        findings.append(
            {
                "field": f"content[type={block_type}]",
                "where": "response",
                "kind": KIND_RESPONSE_BLOCK,
                "count": count,
                "detail": (
                    "a response content block the adapter has no representation for (a server "
                    "tool's own call/result, a citation, a container output). It reaches no "
                    "case and no check."
                ),
                "sample": block_type,
            }
        )

    for status, count in sorted(error_statuses.items()):
        findings.append(
            {
                "field": f"status={status}",
                "where": "response",
                "kind": KIND_ERROR_RESPONSE,
                "count": count,
                "detail": (
                    "the capture contains a non-2xx response. It is preserved in the capture "
                    "and is NOT reproduced by the generated agent: the replay sends the request "
                    "shape, and whether the provider errors is exactly what the run measures. "
                    "Read it as the incident, not as a property of the agent directory."
                ),
                "sample": "",
            }
        )

    upstream = str(index.get("upstream") or "")
    if upstream and "api.anthropic.com" not in upstream:
        findings.append(
            {
                "field": "upstream",
                "where": "capture",
                "kind": KIND_GATEWAY,
                "count": requests,
                "detail": (
                    f"the recorded traffic went to {upstream}, not to the provider. What was "
                    f"recorded is what the GATEWAY received; the gateway may add, rename or "
                    f"reject fields before the provider sees them, and its error wording is "
                    f"its own (rescue-ops A-017: \"only `auto` is supported for `tool_choice`\" "
                    f"is a gateway's sentence, not Anthropic's). A repair verified here is "
                    f"verified against the gateway's contract."
                ),
                "sample": upstream,
            }
        )

    findings.append(
        {
            "field": "/v1/messages/count_tokens, /v1/messages/batches",
            "where": "capture",
            "kind": KIND_UNRECORDED_PATH,
            "count": 0,
            "detail": (
                "the recorder records the Messages path only; token-counting and Batches calls "
                "are relayed without being recorded, so an agent whose defect lives there "
                "cannot be built from a capture (rescue-ops A-058, A-059). Listed on every "
                "capture, because an absence cannot be detected from the recording itself."
            ),
            "sample": "",
        }
    )
    return findings


def _sample(value: Any, limit: int = 200) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def render_markdown(findings: list[dict[str, Any]]) -> list[str]:
    """The ADAPT_EDITS.md section for these findings (a table plus the machine-readable list)."""
    lines = [
        "## Fields the adapter cannot carry (`unsupported_fields.json`)",
        "",
        (
            "Everything the recording held that has no slot in an agent directory. This is not "
            "a list of mistakes — it is the boundary of what a run built on this directory can "
            "measure. A field listed here was on the wire and is not in the replayed request, "
            "so a regression that depends on it cannot be detected, and a `SAFE` verdict says "
            "nothing about it. The same list is written to `unsupported_fields.json` for tools "
            "that want to read rather than render it."
        ),
        "",
        "| field | where | kind | seen | why it matters |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for finding in findings:
        detail = str(finding["detail"]).replace("\n", " ").replace("|", "\\|")
        lines.append(
            f"| `{finding['field']}` | {finding['where']} | `{finding['kind']}` | "
            f"{finding['count']} | {detail} |"
        )
    lines += ["", "```json", json.dumps(findings, indent=1, sort_keys=True), "```", ""]
    return lines
