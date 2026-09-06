"""Repair -> the knob that expresses it, per framework. The machine-readable half of
`docs/framework-mapping.md`; `tests/test_capture_mapping.py` pins the two together.

A capture-derived agent directory names the framework it came from, so when `upshift upgrade`
accepts a repair it can say where that repair lives in the code the user actually maintains.
Without this the deliverable is a diff against an adapter directory they will throw away.

Every value here was read out of the framework's own source or docs at the version recorded in
the doc. `NOT_MAPPED` means the knob does not exist at that version, or could not be verified.
It is never a guess dressed up as one, and it is never silently omitted: a repair with no knob
is a thing the user needs to be told, not a row to hide.
"""

from __future__ import annotations

import json
from pathlib import Path

NOT_MAPPED = "not mapped"

#: Categories a repair falls into. They are the four allowed repair types (SCOPE.md), split
#: where one type reaches two different knobs (`model_params` covers tool_choice, sampling and
#: effort, and those are three unrelated settings in every framework).
TOOL_CHOICE = "tool_choice"
SAMPLING = "sampling"
EFFORT = "effort"
SYSTEM_PROMPT = "system_prompt"
TOOLS = "tools"
ENDPOINT = "endpoint"

CATEGORY_TITLES = {
    TOOL_CHOICE: "forced tool_choice",
    SAMPLING: "temperature / top_p / top_k",
    EFFORT: "reasoning effort",
    SYSTEM_PROMPT: "system prompt",
    TOOLS: "tool schemas",
    ENDPOINT: "endpoint",
}

#: patch id (repair/playbook.py) -> the categories that patch touches, in order.
PATCH_CATEGORIES: dict[str, tuple[str, ...]] = {
    "remove-forced-tool-choice": (TOOL_CHOICE, SYSTEM_PROMPT),
    "drop-sampling-params": (SAMPLING,),
    "raise-effort-one-rung": (EFFORT,),
    "reasoning-effort-high": (EFFORT,),
    "reasoning-effort-none": (EFFORT,),
    "route-to-responses": (ENDPOINT,),
}

#: repair_type -> categories, for a patch id this table does not name.
TYPE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "prompt_edit": (SYSTEM_PROMPT,),
    "tool_schema_edit": (TOOLS,),
    "endpoint_routing": (ENDPOINT,),
    "model_params": (),
}

#: framework -> category -> (what to change, where that was verified).
FRAMEWORKS: dict[str, dict[str, tuple[str, str]]] = {
    "anthropic-sdk-python": {
        TOOL_CHOICE: ("drop the `tool_choice=` argument to `client.messages.create()`",
                      "anthropic/resources/messages/messages.py:137 @1.4.0"),
        SAMPLING: (("already unreachable: 1.x removed temperature/top_p/top_k from the typed "
                   "surface, so only `extra_body={...}` still sends them"),
                   "verified: create(temperature=...) raises TypeError @1.4.0"),
        EFFORT: ("`output_config={\"effort\": \"<level>\"}`",
                 "anthropic/types/output_config_param.py @1.4.0"),
        SYSTEM_PROMPT: ("the `system=` argument to `client.messages.create()`",
                        "anthropic/resources/messages/messages.py:135 @1.4.0"),
        TOOLS: ("the `tools=` argument to `client.messages.create()`",
                "anthropic/resources/messages/messages.py @1.4.0"),
    },
    "anthropic-sdk-typescript": {
        TOOL_CHOICE: ("drop `tool_choice` from the `messages.create()` params",
                      "src/resources/messages/messages.ts:4731 @0.124.0"),
        SAMPLING: (("drop `temperature` / `top_k` / `top_p` — all three are @deprecated with "
                   "this exact 400 documented in the JSDoc"),
                   "src/resources/messages/messages.ts:4712,4818,4825 @0.124.0"),
        EFFORT: ("`output_config: { effort: '<level>' }`",
                 "src/resources/messages/messages.ts:4664,2692 @0.124.0"),
        SYSTEM_PROMPT: ("the `system` param (string or TextBlockParam[])",
                        "src/resources/messages/messages.ts:4705 @0.124.0"),
        TOOLS: ("the `tools` param", "src/resources/messages/messages.ts @0.124.0"),
    },
    "pydantic-ai": {
        TOOL_CHOICE: (("a structured `output_type` forces it; switch to "
                      "`NativeOutput(...)` or `PromptedOutput(...)`, or union the output type "
                      "with `str`. `ModelSettings(tool_choice=...)` does NOT reach output "
                      "tools"),
                      ("pydantic_ai/models/_tool_choice.py:99-101 + models/anthropic.py:"
                      "1914-1923; output.py:21-22 @2.40.0")),
        SAMPLING: ("drop `temperature` / `top_p` / `top_k` from `ModelSettings`",
                   "pydantic_ai/settings.py:140,169,196; models/anthropic.py:585-613 @2.40.0"),
        EFFORT: ("`AnthropicModelSettings(anthropic_effort='<level>')`",
                 "pydantic_ai/models/anthropic.py:502 @2.40.0"),
        SYSTEM_PROMPT: ("`Agent(system_prompt=...)` or `Agent(instructions=...)`",
                        "ai.pydantic.dev agents docs @2.40.0"),
        TOOLS: ("the tool functions registered on the Agent",
                "ai.pydantic.dev tools docs @2.40.0"),
    },
    "litellm": {
        TOOL_CHOICE: (("drop `tool_choice=` from `completion()` (\"required\" is what becomes "
                      "`{\"type\": \"any\"}`)"),
                      "litellm/llms/anthropic/chat/transformation.py:379-419 @1.83.9"),
        SAMPLING: (("omit them, or set `drop_params=True` / "
                   "`additional_drop_params: [\"temperature\"]`"),
                   "litellm/utils.py:2953-2961,3146-3158 @1.83.9"),
        EFFORT: ("`reasoning_effort=\"<level>\"` (mapped to output_config.effort)",
                 "litellm/llms/anthropic/chat/transformation.py:1089-1108 @1.83.9"),
        SYSTEM_PROMPT: ("the `{\"role\": \"system\"}` message litellm hoists into `system`",
                        "litellm/llms/anthropic/chat/transformation.py:1438-1441 @1.83.9"),
        TOOLS: ("the `tools=` argument to `completion()`",
                "litellm/llms/anthropic/chat/transformation.py @1.83.9"),
    },
    "langchain-anthropic": {
        TOOL_CHOICE: (("drop `tool_choice=` from `llm.bind_tools(...)` — note any string other "
                      "than \"any\"/\"auto\" is read as a tool NAME"),
                      "langchain_anthropic/chat_models.py:2064-2091 @0.3.22"),
        SAMPLING: (("leave `temperature` / `top_p` / `top_k` unset on `ChatAnthropic` — None "
                   "values are filtered out of the payload"),
                   "langchain_anthropic/chat_models.py:1373-1380,1618 @0.3.22"),
        EFFORT: (NOT_MAPPED + ": 0.3.22 has no effort/reasoning_effort. The nearest lever is "
                 "`thinking={\"type\": \"enabled\", \"budget_tokens\": N}`",
                 "langchain_anthropic/chat_models.py:1442-1444 @0.3.22"),
        SYSTEM_PROMPT: ("a `SystemMessage` in the message list",
                        "langchain_anthropic/chat_models.py:293-308 @0.3.22"),
        TOOLS: ("the tools passed to `llm.bind_tools(...)`",
                "langchain_anthropic/chat_models.py:2064 @0.3.22"),
    },
    "vercel-ai-sdk": {
        TOOL_CHOICE: (("drop `toolChoice` from the `generateText`/`streamText` call "
                      "('required' is what becomes `{type:'any'}`)"),
                      "@ai-sdk/anthropic anthropic-prepare-tools.ts:390-435 @4.0.49"),
        SAMPLING: ("omit `temperature` / `topP` / `topK` on the call",
                   "@ai-sdk/anthropic anthropic-language-model.ts:566-568 @4.0.49"),
        EFFORT: ("`providerOptions.anthropic.effort`",
                 "@ai-sdk/anthropic anthropic-language-model-options.ts:283 @4.0.49"),
        SYSTEM_PROMPT: (("`instructions` on generateText/streamText (`system` is the "
                        "deprecated alias in ai 7)"),
                        "ai@7.0.93 src/prompt/prompt.ts:19-26"),
        TOOLS: ("the `tools` object on the call",
                "@ai-sdk/anthropic anthropic-prepare-tools.ts @4.0.49"),
    },
    "claude-agent-sdk": {
        TOOL_CHOICE: (NOT_MAPPED + ": the SDK exposes no tool_choice at all, and does not "
                      "send one",
                      ("no occurrence in claude_agent_sdk/types.py @0.2.152 or sdk.d.ts "
                      "@0.3.261")),
        SAMPLING: (NOT_MAPPED + ": none of the three is exposed",
                   "no occurrence in claude_agent_sdk/types.py @0.2.152 or sdk.d.ts @0.3.261"),
        EFFORT: ("`ClaudeAgentOptions(effort=\"<level>\")`",
                 "claude_agent_sdk/types.py:2294 @0.2.152 (sdk.d.ts:1758 @0.3.261)"),
        SYSTEM_PROMPT: (("`ClaudeAgentOptions(system_prompt=...)`, or "
                        "`{\"type\":\"preset\",\"preset\":\"claude_code\",\"append\":\"...\"}` "
                        "— a plain string is appended to the CLI's own prompt, not a "
                        "replacement"),
                        "claude_agent_sdk/types.py:1966-1974 @0.2.152"),
        TOOLS: ("`ClaudeAgentOptions` tool settings / MCP servers",
                "claude_agent_sdk/types.py @0.2.152"),
    },
    "opencode": {
        TOOL_CHOICE: (NOT_MAPPED + " as a config key: opencode sets it internally (only for a "
                      "json_schema output format). The escape hatch is the `chat.params` "
                      "plugin hook",
                      ("packages/opencode/src/session/prompt.ts:1285 and "
                      "session/llm/request.ts:113-130 @bbd72fb")),
        SAMPLING: (("agent-level `temperature` / `top_p`; there is no top_k key. Already unset "
                   "by default for any model id containing 'claude'"),
                   ("packages/opencode/src/agent/agent.ts:41-42 and "
                   "provider/transform.ts:530 @bbd72fb")),
        EFFORT: ("`provider.anthropic.models.<model-id>.options.effort` in opencode.json",
                 "packages/web/src/content/docs/models.mdx:87-97 @bbd72fb"),
        SYSTEM_PROMPT: ("the agent's `prompt`, or the config's `instructions: [...]` files",
                        ("packages/opencode/src/agent/agent.ts:52 and "
                        "packages/web/src/content/docs/config.mdx:817-822 @bbd72fb")),
        TOOLS: ("the agent's `tools` config",
                "packages/web/src/content/docs/agents.mdx @bbd72fb"),
    },
}

#: Where an Anthropic capture's endpoint repair would go: nowhere. /v1/messages is the only
#: endpoint, and the routing repair exists for the OpenAI break.
ENDPOINT_NOTE = (
    "not applicable on Anthropic: /v1/messages is the only endpoint, and the routing repair "
    "exists for the OpenAI chat/completions break"
)

DOC_PATH = "docs/framework-mapping.md"


def categories_for(patch_id: str, repair_type: str) -> tuple[str, ...]:
    """Which knob categories a repair touches."""
    if patch_id in PATCH_CATEGORIES:
        return PATCH_CATEGORIES[patch_id]
    return TYPE_CATEGORIES.get(repair_type, ())


def knob(framework: str, category: str) -> tuple[str, str] | None:
    """(what to change, citation) for one framework and one category, or None if unknown."""
    if category == ENDPOINT:
        return (ENDPOINT_NOTE, DOC_PATH)
    return FRAMEWORKS.get(framework, {}).get(category)


def rows(framework: str, patches: list[dict[str, str]]) -> list[tuple[str, str, str, str]]:
    """(repair id, knob category, what to change, citation) for each accepted repair.

    A repair whose category this framework has no entry for still produces a row, saying so:
    "we changed X and cannot tell you where X lives here" is information, and silence is not.
    """
    out: list[tuple[str, str, str, str]] = []
    for patch in patches:
        patch_id = str(patch.get("id") or "")
        for category in categories_for(patch_id, str(patch.get("repair_type") or "")) or ("",):
            title = CATEGORY_TITLES.get(category, category or "unclassified")
            found = knob(framework, category) if category else None
            if found is None:
                out.append((patch_id, title, NOT_MAPPED, DOC_PATH))
            else:
                out.append((patch_id, title, found[0], found[1]))
    return out


def framework_of(agent_dir: str | Path) -> str | None:
    """The framework an agent directory was built from, or None.

    Only a directory written by `upshift adapt --from-capture` has one: it records the
    detected (or `--framework`-declared) name under agent.json `capture.framework`. A
    hand-written or `upshift adapt`-written directory has no framework, and gets no mapping
    section — upshift will not guess which framework a file it never saw came from.
    """
    path = Path(agent_dir) / "agent.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    name = ((raw.get("capture") or {}) if isinstance(raw.get("capture"), dict) else {}).get(
        "framework"
    )
    if not isinstance(name, str) or not name or name == "unknown":
        return None
    return name


def patch_header(framework: str | None, patches: list[dict[str, str]]) -> str:
    """The mapping as a comment block for the top of `upgrade.patch`, or "".

    The patch edits an adapter directory; the user's actual code is the framework call. Git
    ignores everything before the first `diff --git` line, so the patch still applies, and a
    reader who opens the file sees where the change really belongs before they see the diff.
    """
    mapped = rows(framework or "", patches) if framework else []
    if not mapped:
        return ""
    out = [
        f"# Framework mapping — this patch edits the captured `{framework}` agent directory.",
        (f"# In {framework} itself, the same repairs are these settings "
        f"(citations: {DOC_PATH}):"),
        "#",
    ]
    for patch_id, category, change, citation in mapped:
        out.append(f"#   {patch_id} [{category}]: {change}")
        out.append(f"#     verified at {citation}")
    out.append("#")
    return "\n".join(out) + "\n"
