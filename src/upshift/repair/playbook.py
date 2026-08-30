"""Repair candidate generation. Rule-based playbook keyed on failure signatures.

Repairs are limited to the four allowed types (SCOPE.md): prompt edits, model params,
tool schema edits, endpoint routing. Candidates are whole-file replacements of the victim's
three patchable files, ordered by how directly they target the observed signature.

The marker phrases inside the text blocks below are contracts with providers/sim.py
(corruption suppression) — change them only together.

The blocks are domain-neutral on purpose: they are appended verbatim to whatever agent is
under repair, so nothing here may name a tool or a business domain. The single exception is
the tool-schema edit, which targets a tool by name and is skipped when that tool is absent
(see ``_tool_description_edit``).
"""

from __future__ import annotations

import json
from pathlib import Path

from upshift.schemas import FileEdit, Patch

DISCIPLINE_BLOCK = (
    "\n\nExecution discipline: call each tool at most once per user request unless the "
    "previous call failed. Never repeat a tool call that already succeeded."
)
STOP_BLOCK = (
    "\n\nStop once the task is complete. After the user's goal has been achieved, do not "
    "make any further tool calls; reply to the user instead."
)
NO_FABRICATION_BLOCK = (
    "\n\nNever state a confirmation number or any other identifier that was not returned by "
    "a tool call in this conversation. If the tool that produces it has not been called yet, "
    "call it before confirming anything to the user."
)
#: The only tool name the playbook knows. Every candidate that mentions it is skipped for an
#: agent that does not expose a tool with this exact name (see ``_tool_description_edit``).
BOOK_TOOL_NAME = "book_flight"
# Appended to that tool's description; skipped when the tool is absent.
BOOK_TOOL_SUFFIX = (
    " This tool must be called to create any real booking. Call it exactly once per "
    "confirmed itinerary; never confirm a booking without calling it."
)
# Observed on real gpt-5.6-sol (pilot 2026-08-28): interrogates instead of executing —
# asks for details the tool does not even require, then acts on nothing.
EXECUTE_BLOCK = (
    "\n\nWhen the user's request already contains everything a tool needs, call the "
    "tool immediately instead of asking for more information. Never ask for details that "
    "a tool's schema does not require."
)
# Observed on real gpt-5.6-sol (full run 2026-08-28): announces "nothing available"
# after its own search returned results, then refuses to act.
RESULTS_GROUNDING_BLOCK = (
    "\n\nBase every availability or status statement strictly on the tool results in this "
    "conversation. If a tool returned one or more results, those results exist and are "
    "usable; never claim nothing is available when the results list is non-empty."
)
# Observed on real gpt-5.6-sol (pilot 2026-08-28): reformats identifiers ("B6 220" for
# "B6220"), breaking exact-output contracts downstream systems rely on.
VERBATIM_BLOCK = (
    "\n\nWrite every identifier a tool returned exactly as the tool returned it, character "
    "for character. Never insert spaces into or otherwise reformat an identifier."
)


def _read(agent_dir: Path, rel: str) -> str:
    return (agent_dir / rel).read_text()


def _json_key_edit(text: str, key: str, old_value, new_value) -> str | None:
    """Replace `"key": <old>` with `"key": <new>` textually so the emitted git diff touches
    only the changed line, never reformatting the rest of the file. Returns None when the
    fragment is not found exactly once (caller falls back to a full re-serialize)."""
    fragment = f"{json.dumps(key)}: {json.dumps(old_value)}"
    if text.count(fragment) == 1:
        return text.replace(fragment, f"{json.dumps(key)}: {json.dumps(new_value)}")
    return None


def _agent_json_edit(agent_dir: Path, key_path: list[str], new_value) -> FileEdit:
    text = _read(agent_dir, "agent.json")
    raw = json.loads(text)
    node = raw
    for key in key_path[:-1]:
        node = node.setdefault(key, {})
    minimal = (
        _json_key_edit(text, key_path[-1], node[key_path[-1]], new_value)
        if key_path[-1] in node
        else None
    )
    if minimal is not None:
        return FileEdit(file="agent.json", new_content=minimal)
    node[key_path[-1]] = new_value
    return FileEdit(file="agent.json", new_content=json.dumps(raw, indent=2) + "\n")


def _prompt_append(agent_dir: Path, raw_config: dict, block: str) -> FileEdit:
    rel = raw_config["system_prompt_file"]
    return FileEdit(file=rel, new_content=_read(agent_dir, rel).rstrip("\n") + block + "\n")


def _tool_description_edit(
    agent_dir: Path, raw_config: dict, tool_name: str, suffix: str
) -> FileEdit | None:
    """Append `suffix` to one named tool's description.

    Returns None — and the caller then drops the candidate entirely — when the agent has no
    such tool or its description already carries the marker. This is the one victim-flavored
    repair: it fires only for an agent that literally exposes a tool called `tool_name`, and
    is a silent no-op for every other agent.
    """
    rel = raw_config["tools_file"]
    text = _read(agent_dir, rel)
    tools = json.loads(text)
    for tool in tools:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        if fn.get("name") == tool_name:
            old_desc = fn.get("description", "")
            if "exactly once" in old_desc.lower():
                return None
            new_desc = old_desc.rstrip() + suffix
            minimal = (
                text.replace(json.dumps(old_desc), json.dumps(new_desc))
                if old_desc and text.count(json.dumps(old_desc)) == 1
                else None
            )
            if minimal is not None:
                return FileEdit(file=rel, new_content=minimal)
            fn["description"] = new_desc
            return FileEdit(file=rel, new_content=json.dumps(tools, indent=2) + "\n")
    return None


def generate_candidates(agent_dir: str | Path, signatures: list[str]) -> list[Patch]:
    """Ordered repair candidates for the observed failure signatures, computed against the
    CURRENT contents of agent_dir (so candidates stack across repair iterations)."""
    agent_dir = Path(agent_dir)
    raw_config = json.loads(_read(agent_dir, "agent.json"))
    prompt = _read(agent_dir, raw_config["system_prompt_file"]).lower()
    candidates: list[Patch] = []

    def add(patch_id, repair_type, signature, description, edits):
        edits = [e for e in edits if e is not None]
        if edits:
            candidates.append(Patch(patch_id, repair_type, signature, description, edits))

    for sig in signatures:
        if sig == "api_error_tools_reasoning":
            if raw_config["endpoint"] != "responses":
                add(
                    "route-to-responses",
                    "endpoint_routing",
                    sig,
                    "Route API calls from /v1/chat/completions to /v1/responses "
                    "(function tools + reasoning_effort are rejected on chat/completions "
                    "for this model family).",
                    [_agent_json_edit(agent_dir, ["endpoint"], "responses")],
                )
            if raw_config.get("params", {}).get("reasoning_effort") != "none":
                add(
                    "reasoning-effort-none",
                    "model_params",
                    sig,
                    "Set reasoning_effort='none' to keep function tools working on "
                    "/v1/chat/completions (documented alternative to endpoint routing).",
                    [_agent_json_edit(agent_dir, ["params", "reasoning_effort"], "none")],
                )
        elif sig == "duplicate_tool_calls":
            if "at most once" not in prompt and "exactly once" not in prompt:
                add(
                    "prompt-execution-discipline",
                    "prompt_edit",
                    sig,
                    "Append an execution-discipline block: each tool at most once per "
                    "request, never repeat a successful call.",
                    [_prompt_append(agent_dir, raw_config, DISCIPLINE_BLOCK)],
                )
            add(
                "tool-schema-book-once",
                "tool_schema_edit",
                sig,
                f"Strengthen the {BOOK_TOOL_NAME} description: exactly once per confirmed "
                "itinerary (skipped when the agent has no such tool).",
                [_tool_description_edit(agent_dir, raw_config, BOOK_TOOL_NAME, BOOK_TOOL_SUFFIX)],
            )
        elif sig == "acting_past_goal":
            if "stop once the task is complete" not in prompt:
                add(
                    "prompt-stop-after-goal",
                    "prompt_edit",
                    sig,
                    "Append a stop-after-goal block: no further tool calls once the goal "
                    "is achieved.",
                    [_prompt_append(agent_dir, raw_config, STOP_BLOCK)],
                )
        elif sig == "skipped_tool_hallucination":
            if "never state a confirmation number" not in prompt:
                add(
                    "prompt-no-fabrication",
                    "prompt_edit",
                    sig,
                    "Append a no-fabrication block: never state a confirmation number a "
                    "tool did not return; call the tool instead.",
                    [_prompt_append(agent_dir, raw_config, NO_FABRICATION_BLOCK)],
                )
            add(
                "tool-schema-book-required",
                "tool_schema_edit",
                sig,
                f"Strengthen the {BOOK_TOOL_NAME} description: must be called to create any "
                "booking (skipped when the agent has no such tool).",
                [_tool_description_edit(agent_dir, raw_config, BOOK_TOOL_NAME, BOOK_TOOL_SUFFIX)],
            )
        elif sig in ("wrong_or_missing_tool_call", "other_behavioral"):
            if sig == "wrong_or_missing_tool_call" and "call the tool immediately" not in prompt:
                add(
                    "prompt-execute-dont-ask",
                    "prompt_edit",
                    sig,
                    "Append an execute-don't-interrogate block: when a request already "
                    "contains everything a tool needs, call it instead of asking for "
                    "details the tool does not require (observed on real gpt-5.6-sol).",
                    [_prompt_append(agent_dir, raw_config, EXECUTE_BLOCK)],
                )
            if (
                sig == "wrong_or_missing_tool_call"
                and "strictly on the tool results" not in prompt
            ):
                add(
                    "prompt-ground-in-results",
                    "prompt_edit",
                    sig,
                    "Append a results-grounding block: never claim nothing is available "
                    "when search returned flights (observed on real gpt-5.6-sol).",
                    [_prompt_append(agent_dir, raw_config, RESULTS_GROUNDING_BLOCK)],
                )
            if sig == "other_behavioral" and "character for character" not in prompt:
                add(
                    "prompt-verbatim-identifiers",
                    "prompt_edit",
                    sig,
                    "Append a verbatim-identifiers block: report flight/booking ids "
                    "exactly as tools returned them (observed on real gpt-5.6-sol).",
                    [_prompt_append(agent_dir, raw_config, VERBATIM_BLOCK)],
                )
            if raw_config.get("params", {}).get("reasoning_effort") not in ("high",):
                add(
                    "reasoning-effort-high",
                    "model_params",
                    sig,
                    "Raise reasoning_effort to 'high' (fallback for unclassified "
                    "behavioral failures).",
                    [_agent_json_edit(agent_dir, ["params", "reasoning_effort"], "high")],
                )

    # De-duplicate by patch id AND by resulting content (two signatures can propose the
    # same edit; trying identical content twice would waste repair budget).
    seen: set[str] = set()
    unique = []
    for patch in candidates:
        content_key = "|".join(f"{e.file}:{hash(e.new_content)}" for e in patch.edits)
        if patch.id not in seen and content_key not in seen:
            seen.update((patch.id, content_key))
            unique.append(patch)
    return unique
