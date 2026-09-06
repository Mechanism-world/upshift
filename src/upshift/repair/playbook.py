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

# ---------------------------------------------------------------------------
# Documented Claude Fable 5 -> 5.1 repairs (DESIGN.md, "Documented 5 -> 5.1 changes as
# detectors + repairs"). Every sentence below is the documented wording, VERBATIM; it is
# appended to the system prompt because that is the only placement the allowed repair types
# permit. Do not paraphrase — the wording is the repair.
# ---------------------------------------------------------------------------

#: Instruction-based replacement for a `tool_choice` that named one tool (item 1).
FORCED_TOOL_INSTRUCTION = "Use the `{tool}` tool to answer; call it rather than replying in text."
#: Instruction-based replacement for `any` / `required` (item 1).
FORCED_ANY_INSTRUCTION = (
    "Respond with a tool call rather than text whenever one of the tools applies."
)
#: Documented batching sentence for lost parallelism (item 3).
BATCH_BLOCK = (
    "\n\nFirst privately list what you need next; then request every item that doesn't "
    "depend on another's result in this one response."
)
#: Documented verification nudge for dropped retrieval calls (item 4b).
VERIFICATION_NUDGE_BLOCK = (
    "\n\nWhen a query centers on a name you do not confidently recognize, or recognize from "
    "a fast-moving area like AI models and developer tools where the landscape shifts within "
    "months, the name itself is the thing to verify: search before answering, and include the "
    "name as the user wrote it in at least one query alongside any reformulations. This holds "
    "even when you have some background on it — partial background is exactly what makes an "
    "out-of-date answer sound authoritative, so familiarity is not a reason to skip the search."
)

#: Legal reasoning-effort ladders per endpoint, lowest first, and the value an ABSENT param
#: means on that endpoint (DESIGN.md: Fable defaults to `high`; the OpenAI endpoints default
#: to `medium`). Effort is only ever raised — lowering it is never a regression repair.
EFFORT_LADDERS = {
    "messages": ("low", "medium", "high", "xhigh", "max"),
    "chat_completions": ("none", "low", "medium", "high"),
    "responses": ("none", "low", "medium", "high"),
}
EFFORT_WHEN_UNSET = {"messages": "high", "chat_completions": "medium", "responses": "medium"}
#: Params both Fables reject at non-default values (item 5).
SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


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


def _value_end(text: str, start: int) -> int | None:
    """Index just past the JSON value starting at/after `start`; None if it is malformed.

    Scans strings, objects and arrays with nesting so a value written on one line
    (``{"tool_choice": {"type": "tool", "name": "x"}}``) is handled exactly like a
    pretty-printed one. Scalars end at the first comma or closing bracket.
    """
    i = start
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text):
        return None
    depth = 0
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                if depth == 0:
                    return i + 1
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            if depth == 0:
                return i  # scalar terminated by the enclosing object/array
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch == "," and depth == 0:
            return i
        i += 1
    return None


def _json_key_remove(text: str, key: str) -> str | None:
    """Delete `"key": <value>` (and its separating comma) textually, so the emitted git diff
    touches only that key. Returns None when the key is absent, appears more than once, or the
    result would not be valid JSON — the caller then re-serializes the whole file."""
    needle = json.dumps(key) + ":"
    if text.count(needle) != 1:
        return None
    start = text.index(needle)
    end = _value_end(text, start + len(needle))
    if end is None:
        return None

    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start  # the key owns its line: drop the indentation too

    after = end
    while after < len(text) and text[after] in " \t":
        after += 1
    if after < len(text) and text[after] == ",":
        after += 1
        while after < len(text) and text[after] in " \t":
            after += 1
        if after < len(text) and text[after] == "\n" and start == line_start:
            after += 1
        candidate = text[:start] + text[after:]
    else:
        # Last entry of its object: the comma to remove is the one BEFORE the key.
        before = start
        while before > 0 and text[before - 1] in " \t\r\n":
            before -= 1
        if before > 0 and text[before - 1] == ",":
            before -= 1
        candidate = text[:before] + text[end:]

    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return candidate


def _agent_json_remove(
    agent_dir: Path, param_keys: list[str], *, also_extra_body: bool = False
) -> FileEdit | None:
    """Remove one or more keys from ``params`` in agent.json.

    With ``also_extra_body`` the same keys are removed from ``params.extra_body`` as well, and
    an extra_body emptied by that is removed with them. That hatch is where an agent (or
    upshift's own params mapping) puts a sampling parameter the installed SDK will not take as
    a keyword, so a repair that could not see inside it fired its signature and produced no
    candidate at all. What the agent DECLARES is one thing and how it TRAVELS is another; the
    repair is about the declaration, in either spelling.

    The same keys are removed from every ``turn_params`` entry too, because a param declared
    per turn is still declared: a capture-derived agent typically carries its forced
    `tool_choice` there and nowhere else, and half a removal is not a repair. A sequence left
    with nothing to say is dropped rather than kept as ``[{}, {}]``.

    Returns None when the agent declares none of them (nothing to repair, candidate dropped).
    Minimal textual deletions are chained; any key that resists a clean textual deletion — or
    that would leave an empty extra_body behind — falls the whole file back to a re-serialize,
    which is still a correct patch.
    """
    text = _read(agent_dir, "agent.json")
    raw = json.loads(text)
    params = raw.get("params")
    params = params if isinstance(params, dict) else {}
    extra_body = params.get("extra_body") if also_extra_body else None
    extra_body = extra_body if isinstance(extra_body, dict) else {}
    turn_params = [e for e in (raw.get("turn_params") or []) if isinstance(e, dict)]
    present = [k for k in param_keys if k in params]
    nested = [k for k in param_keys if k in extra_body]
    per_turn = [k for k in param_keys if any(k in entry for entry in turn_params)]
    if not present and not nested and not per_turn:
        return None
    minimal: str | None = None if per_turn else text
    for key in present + nested:
        minimal = _json_key_remove(minimal, key) if minimal is not None else None
    if minimal is not None and not _leaves_an_empty_extra_body(minimal):
        return FileEdit(file="agent.json", new_content=minimal)
    for key in present:
        params.pop(key, None)
    for key in nested:
        extra_body.pop(key, None)
    if nested and not extra_body:
        params.pop("extra_body", None)
    for entry in turn_params:
        for key in per_turn:
            entry.pop(key, None)
    if turn_params and not any(turn_params):
        raw.pop("turn_params", None)
    return FileEdit(file="agent.json", new_content=json.dumps(raw, indent=2) + "\n")


def _leaves_an_empty_extra_body(text: str) -> bool:
    """True when a textual deletion emptied ``params.extra_body``; the re-serialize path drops
    the husk instead of leaving `"extra_body": {}` in the patch."""
    params = json.loads(text).get("params")
    return isinstance(params, dict) and params.get("extra_body") == {}


def _effort_ladder(endpoint: str) -> tuple[tuple[str, ...], str]:
    """(ladder, value-an-absent-param-means) for an endpoint; empty ladder if unknown."""
    return EFFORT_LADDERS.get(endpoint, ()), EFFORT_WHEN_UNSET.get(endpoint, "")


def _next_effort(raw_config: dict) -> str | None:
    """The next rung up the endpoint's ladder, or None when already at the top / off-ladder."""
    ladder, unset = _effort_ladder(raw_config.get("endpoint", ""))
    if not ladder:
        return None
    current = raw_config.get("params", {}).get("reasoning_effort") or unset
    if current not in ladder:
        return None
    index = ladder.index(current)
    return ladder[index + 1] if index + 1 < len(ladder) else None


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


def _forced_tool_choice_instruction(tool_choice) -> str | None:
    """The prompt sentence that replaces a forced `tool_choice`, or None if it is not forced.

    Handles both shapes: Anthropic (`{"type": "any"}`, `{"type": "tool", "name": X}`) and
    OpenAI (`"required"`, `{"type": "function", "function": {"name": X}}`). `auto`/`none` are
    not forced choices and produce no candidate.
    """
    if isinstance(tool_choice, str):
        return FORCED_ANY_INSTRUCTION if tool_choice == "required" else None
    if not isinstance(tool_choice, dict):
        return None
    kind = tool_choice.get("type")
    if kind == "any":
        return FORCED_ANY_INSTRUCTION
    name = None
    if kind == "tool":
        name = tool_choice.get("name")
    elif kind == "function":
        name = (tool_choice.get("function") or {}).get("name")
    if isinstance(name, str) and name:
        return FORCED_TOOL_INSTRUCTION.format(tool=name)
    return None


def _declared_forced_tool_choice(raw_config: dict) -> str | None:
    """The prompt sentence for whatever forced `tool_choice` the agent declares, anywhere.

    A capture-derived agent may hold it in `turn_params` rather than `params` — that is what
    the sequence is FOR, since a framework typically forces the first turn only. A repair
    that read `params` alone would find nothing, drop its own candidate, and leave a break
    upshift knows how to fix looking unfixable.
    """
    declared = [(raw_config.get("params") or {}).get("tool_choice")]
    declared += [
        entry.get("tool_choice")
        for entry in (raw_config.get("turn_params") or [])
        if isinstance(entry, dict)
    ]
    for value in declared:
        instruction = _forced_tool_choice_instruction(value)
        if instruction is not None:
            return instruction
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

    def add_effort_rung(sig: str) -> None:
        """Raise reasoning effort ONE rung on the endpoint's own ladder (DESIGN item 6)."""
        nxt = _next_effort(raw_config)
        if nxt is None:
            return
        add(
            "raise-effort-one-rung",
            "model_params",
            sig,
            f"Raise reasoning_effort one rung to {nxt!r} on the {raw_config['endpoint']} "
            "ladder (effort is only ever raised, never lowered).",
            [_agent_json_edit(agent_dir, ["params", "reasoning_effort"], nxt)],
        )

    for sig in signatures:
        if sig == "api_error_forced_tool_choice":
            instruction = _declared_forced_tool_choice(raw_config)
            removal = (
                _agent_json_remove(agent_dir, ["tool_choice"]) if instruction is not None else None
            )
            if instruction is not None and removal is not None:
                add(
                    "remove-forced-tool-choice",
                    "model_params",
                    sig,
                    "Drop the forced tool_choice param (rejected by this model) and state the "
                    "same requirement as an instruction in the system prompt instead.",
                    [removal, _prompt_append(agent_dir, raw_config, "\n\n" + instruction)],
                )
        elif sig == "api_error_unsupported_sampling_params":
            add(
                "drop-sampling-params",
                "model_params",
                sig,
                "Remove temperature / top_p / top_k: this model rejects non-default sampling "
                "params (typically left over from an OpenAI-style config).",
                [_agent_json_remove(agent_dir, list(SAMPLING_PARAMS), also_extra_body=True)],
            )
        elif sig == "serialized_tool_calls":
            if "in this one response" not in prompt:
                add(
                    "prompt-batch-tool-calls",
                    "prompt_edit",
                    sig,
                    "Append the documented batching instruction: plan first, then request "
                    "every independent item in one response.",
                    [_prompt_append(agent_dir, raw_config, BATCH_BLOCK)],
                )
            add_effort_rung(sig)
        elif sig == "reduced_retrieval_calls":
            # DESIGN item 4 orders these: effort first, then the documented nudge.
            add_effort_rung(sig)
            if "the name itself is the thing to verify" not in prompt:
                add(
                    "prompt-verification-nudge",
                    "prompt_edit",
                    sig,
                    "Append the documented verification nudge: verify unfamiliar names by "
                    "searching before answering.",
                    [_prompt_append(agent_dir, raw_config, VERIFICATION_NUDGE_BLOCK)],
                )
        elif sig == "api_error_tools_reasoning":
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
            add_effort_rung(sig)

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
