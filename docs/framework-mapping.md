# Framework mapping

upshift repairs a *request*. This table says where that repair lives in the framework that
built the request, so a `SAFE WITH PATCH` verdict is something a user can act on rather than a
diff against an adapter directory they will throw away.

**How to read it.** Every cell was verified by reading the framework's own source or docs at
the version named below, and carries the citation. A cell that says **not mapped** means the
knob does not exist at that version, or could not be verified — never that we guessed and
guessed "no". `upshift capture` records the `user-agent` it saw in `index.json`, so a mapping
can always be checked against the bytes.

Verified 2026-09-05, at these versions:

| framework | version inspected |
| --- | --- |
| anthropic-sdk-python | `anthropic` 1.4.0 (PyPI) |
| anthropic-sdk-typescript | `@anthropic-ai/sdk` 0.124.0 (npm, latest dist-tag) |
| pydantic-ai | `pydantic-ai-slim` 2.40.0 (PyPI) |
| litellm | `litellm` 1.83.9 (PyPI) |
| langchain-anthropic | `langchain-anthropic` 0.3.22 (PyPI) |
| vercel-ai-sdk | `@ai-sdk/anthropic` 4.0.49 with `ai` 7.0.93 |
| claude-agent-sdk | python 0.2.152 (bundles `claude-cli/2.1.259`), TS 0.3.261 |
| opencode | `sst/opencode` @ `bbd72fb8b0bb6de580d2041a0150016227c63ac0`, pins `@ai-sdk/anthropic` 3.0.111 |

---

## 1. Pointing the framework at `upshift capture`

Start the recorder, then set **one** of these. All eight can be redirected — seven by an
environment variable, opencode by config only.

| framework | setting | note |
| --- | --- | --- |
| anthropic-sdk-python | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`, or `Anthropic(base_url=…)` | read at `anthropic/_client.py:234` (sync) and `:660` (async) @1.4.0 |
| anthropic-sdk-typescript | `ANTHROPIC_BASE_URL=…`, or `new Anthropic({ baseURL })` | `src/client.ts:610` @0.124.0 |
| pydantic-ai | `ANTHROPIC_BASE_URL=…`, or `AnthropicProvider(base_url=…)` | `pydantic_ai/providers/anthropic.py:166,171` @2.40.0 — `base_url=None` falls through to the SDK's env var |
| litellm | `ANTHROPIC_API_BASE=…` or `ANTHROPIC_BASE_URL=…`, or `api_base=` / `litellm_params.api_base` | `litellm/main.py:2848-2853` @1.83.9. **litellm appends `/v1/messages` itself** (`main.py:2856-2862`), so give it the bare origin |
| langchain-anthropic | `ANTHROPIC_API_URL=…`, else `ANTHROPIC_BASE_URL=…`, else `ChatAnthropic(base_url=…)` | `langchain_anthropic/chat_models.py:1393-1398` @0.3.22 |
| vercel-ai-sdk | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787/v1`, or `createAnthropic({ baseURL })` | `anthropic-provider.ts:118-124` @4.0.49. **Include the `/v1`**: `normalizeBaseURL` (`:28-35`) adds it only for the literal `https://api.anthropic.com`, and the request is `${baseURL}/messages` (`anthropic-language-model.ts:964-968`) |
| claude-agent-sdk | `ANTHROPIC_BASE_URL=…` in the process env, or `ClaudeAgentOptions(env={…})` | it spawns the `claude` CLI and inherits the environment: `claude_agent_sdk/types.py:2084`, merged at `_internal/transport/subprocess_cli.py:809-814` @0.2.152; TS `Options.env` at `sdk.d.ts:1525` @0.3.261 |
| opencode | `provider.anthropic.options.baseURL` in `opencode.json` (`provider.anthropic.api` when the entry also pins a `npm` package) — **no environment variable** | `packages/web/src/content/docs/providers.mdx:32-47` @`bbd72fb`. opencode reads no base-URL env var of its own; `options` is passed straight to `createAnthropic` (`packages/opencode/src/provider/provider.ts:1755-1776`). Same `/v1` note as the AI SDK: it requests `baseURL + "/messages"` |

`upshift capture` accepts both `POST /v1/messages` and the AI SDK's `POST /messages`, and
forwards the versioned path upstream either way — but setting the base URL with `/v1` is the
shape those two projects document for themselves.

Two of these also carry a query string (`POST /v1/messages?beta=true`): pydantic-ai always
uses the beta endpoint, and so does the Claude Agent SDK. The recorder ignores the query when
deciding what to record, and keeps it verbatim in the request record.

---

## 2. Where each repair lives

The four repair types upshift may emit (SCOPE.md) against the knob that expresses them.

### Remove a forced `tool_choice` (and state the requirement in the prompt instead)

The repair for `api_error_forced_tool_choice` — the 400
`tool_choice: type "tool" and "any" are not supported for this model.`

| framework | knob |
| --- | --- |
| anthropic-sdk-python | `client.messages.create(tool_choice=…)` — `anthropic/resources/messages/messages.py:137` |
| anthropic-sdk-typescript | `tool_choice?: ToolChoice` — `src/resources/messages/messages.ts:4731` (union at `:3419`) |
| pydantic-ai | **See the note below — a structured `output_type` forces it, and `ModelSettings(tool_choice=…)` does not reach it.** Use `pydantic_ai.output.NativeOutput` or `PromptedOutput` (`pydantic_ai/output.py:21-22`), or union the output type with `str` |
| litellm | `tool_choice=` on `completion()`; `"required"` → `{"type": "any"}` — `litellm/llms/anthropic/chat/transformation.py:379-419` |
| langchain-anthropic | `llm.bind_tools(tools, tool_choice=…)` — `chat_models.py:2064-2091`. Careful: any string other than `"any"`/`"auto"` is read as a **tool name** |
| vercel-ai-sdk | `toolChoice` on `generateText` / `streamText` — `anthropic-prepare-tools.ts:390-435` (`'required'` → `{type:'any'}`; `'none'` drops the `tools` array entirely) |
| claude-agent-sdk | **not mapped** — no `tool_choice` / `toolChoice` anywhere in `claude_agent_sdk/types.py` @0.2.152 or `sdk.d.ts` @0.3.261, and captured requests carry none |
| opencode | **not mapped** — set internally only (`packages/opencode/src/session/prompt.ts:1285`, `"required"` for a `json_schema` output format), and there is no user-side escape hatch: the `chat.params` plugin hook carries no tool-choice field and is never read back for one (`packages/plugin/src/index.ts:247-256` @`v1.18.29`). Changing it means patching the source. Reaching this path to record it: opencode's own documented structured-output API, `POST /session/:id/message` with `body.format = {type:"json_schema", schema}` (`packages/web/src/content/docs/sdk.mdx:120-152` @`v1.18.29`); note the checked-in `types.gen.ts` omits `format`, so use the HTTP API rather than the typed SDK |

**pydantic-ai, in detail** (this is the single most common shape of the break). A structured
`output_type` sets `tool_choice: {"type": "any"}` on the Anthropic request:
`resolve_tool_choice` returns `'required'` when the output schema allows no text output
(`pydantic_ai/models/_tool_choice.py:99-101`), and `models/anthropic.py:1914-1923` maps
`'required'` → `{'type': 'any'}`. Measured against a recorder: `output_type=Out` and
`ToolOutput(Out)` both send `{"type":"any"}`; `NativeOutput(Out)` and `PromptedOutput(Out)`
send no `tool_choice` at all; `[Out, str]` softens it to `{"type":"auto"}`.
`ModelSettings(tool_choice=…)` does **not** help — its own docstring says it "controls
function tools only" (`models/_tool_choice.py:20-22`).

pydantic-ai ≥2.40 also fixes this itself for models it recognises: the profile flag
`anthropic_supports_forced_tool_choice` (`profiles/anthropic.py:114-121`, computed at `:255`
as `not model_name.startswith(('claude-fable-5-1', 'claude-mythos-5-1'))`) degrades a forced
choice to `{'type':'auto'}`. So on a recognised model name the repair is already applied
upstream; on an unrecognised one it is not, and the lever is a custom `profile=` passed to
`AnthropicModel`.

### Drop `temperature` / `top_p` / `top_k`

The repair for `api_error_unsupported_sampling_params`.

| framework | knob |
| --- | --- |
| anthropic-sdk-python | **already impossible to send normally**: 1.x removed all three from the typed surface — `create(temperature=…)` raises `TypeError`. Only `extra_body={"temperature": …}` still reaches the wire |
| anthropic-sdk-typescript | still typed, all three `@deprecated` with the 400 documented in the JSDoc — `messages.ts:4712` (`temperature`), `:4818` (`top_k`), `:4825` (`top_p`) |
| pydantic-ai | `ModelSettings(temperature=…, top_p=…, top_k=…)` — `settings.py:140/169/196`, shipped via `extra_body` (`models/anthropic.py:585-613`); auto-dropped with a `UserWarning` when the profile says the model refuses them (`models/anthropic.py:1082-1105`) |
| litellm | omit them, or `drop_params=True` / `additional_drop_params: ["temperature"]` — `litellm/utils.py:2953-2961`, `:3146-3158`. Note `top_k` is not in litellm's supported-params list (`chat/transformation.py:217-249`) but still reaches the wire as a provider-specific kwarg (`utils.py:4799-4807`) |
| langchain-anthropic | leave the constructor fields `None` — `chat_models.py:1373-1380`; the payload is filtered `if v is not None` at `:1618` |
| vercel-ai-sdk | omit `temperature` / `topP` / `topK` on the call — `anthropic-language-model.ts:566-568` |
| claude-agent-sdk | **not mapped** — none of the three is exposed |
| opencode | agent-level `temperature` / `top_p` (`packages/opencode/src/agent/agent.ts:41-42`); no `top_k` key exists. Already `undefined` by default for any model id containing `claude` (`packages/opencode/src/provider/transform.ts:530`) |

### Raise reasoning effort

The repair for `reduced_retrieval_calls` and `serialized_tool_calls` — one rung up
`low < medium < high < xhigh < max`.

| framework | knob |
| --- | --- |
| anthropic-sdk-python | `output_config={"effort": "xhigh"}` — `anthropic/types/output_config_param.py`; `thinking=` at `messages.py:136` |
| anthropic-sdk-typescript | `output_config?: OutputConfig` — `messages.ts:4664`, `effort` at `:2692`; `thinking?` at `:4725` |
| pydantic-ai | `AnthropicModelSettings(anthropic_effort='xhigh')` — `models/anthropic.py:502`; `anthropic_thinking={…}` at `:442` |
| litellm | `reasoning_effort="xhigh"` — mapped to `output_config.effort` for 4.6/4.7-class models at `chat/transformation.py:1089-1108`, which also adds `anthropic-beta: effort-2025-11-24` |
| langchain-anthropic | **not mapped** — no `effort` / `reasoning_effort` symbol exists in 0.3.22. The nearest lever is `thinking={"type": "enabled", "budget_tokens": N}` (`chat_models.py:1442-1444`) or a raw `model_kwargs` passthrough (`:1613-1614`) |
| vercel-ai-sdk | `providerOptions.anthropic.effort` — `anthropic-language-model-options.ts:283`, emitted as `output_config.effort` at `anthropic-language-model.ts:585-593` |
| claude-agent-sdk | `ClaudeAgentOptions(effort="xhigh")` — `types.py:2294` (TS `sdk.d.ts:1758`); `thinking=` at `types.py:2281` |
| opencode | `provider.anthropic.models.<model-id>.options.effort` (or `.thinking`) — `packages/web/src/content/docs/models.mdx:87-97`; agent-level keys pass through as provider options |

### Append a sentence to the system prompt

The repair type behind every `prompt_edit` candidate, and the second half of the forced
`tool_choice` repair.

| framework | knob |
| --- | --- |
| anthropic-sdk-python | `messages.create(system=…)` — `messages.py:135` |
| anthropic-sdk-typescript | `system?: string \| Array<TextBlockParam>` — `messages.ts:4705` |
| pydantic-ai | `Agent(system_prompt=…)` / `Agent(instructions=…)` / `@agent.instructions` |
| litellm | a `{"role": "system"}` message; litellm hoists it into `system` — `chat/transformation.py:1438-1441` |
| langchain-anthropic | a `SystemMessage` in the message list — `chat_models.py:293-308` |
| vercel-ai-sdk | `instructions` on `generateText` / `streamText` (`system` is the deprecated alias in `ai` 7 — `src/prompt/prompt.ts:19-26`) |
| claude-agent-sdk | `ClaudeAgentOptions(system_prompt=…)`, or `{"type":"preset","preset":"claude_code","append":"…"}` to append after the Claude Code prompt — `types.py:1966-1974`. A plain string is still **appended** to the CLI's own prompt, never a replacement |
| opencode | agent `prompt` (`packages/opencode/src/agent/agent.ts:52`) and config `instructions: [...]` (`packages/web/src/content/docs/config.mdx:817-822`), assembled at `session/llm/request.ts:59-66` |

### Endpoint routing

Not applicable on Anthropic: `/v1/messages` is the only endpoint, and the routing repair
(`chat/completions` → `responses`) exists for the OpenAI break only.

---

## 3. Identifying a framework from a capture

What each one puts in `user-agent`, verified from source and — where marked — observed against
a recorder. `upshift capture` uses these; `--framework <name>` overrides them.

| framework | `user-agent` | other markers |
| --- | --- | --- |
| anthropic-sdk-python | `Anthropic/Python 1.4.0` (observed) | `x-stainless-lang: python`, `x-stainless-package-version`, `x-stainless-runtime: CPython` |
| anthropic-sdk-typescript | `Anthropic/JS 0.124.0` (source only: `src/client.ts:960-962`) | `x-stainless-lang: js` |
| pydantic-ai | `pydantic-ai/2.40.0` (observed) — it overrides the SDK's UA (`pydantic_ai/_http.py:64`) | still carries `x-stainless-*`; path `POST /v1/messages?beta=true` |
| litellm | `litellm/1.83.9` (`llms/custom_httpx/http_handler.py:56-68`; override with `LITELLM_USER_AGENT`) | — |
| langchain-anthropic | **none of its own** — `Anthropic/Python 0.125.0`, byte-identical to a raw SDK call | pass `--framework langchain-anthropic` |
| vercel-ai-sdk | `ai-sdk/anthropic/4.0.49 ai-sdk/provider-utils/5.0.36 runtime/…`, or `ai/7.0.93 …` when called through `generateText` | — |
| claude-agent-sdk | `claude-cli/2.1.259 (external, sdk-py, agent-sdk/0.2.152)` (observed) | `x-app: cli`, `x-claude-code-session-id`; path `?beta=true` |
| opencode | `opencode/<version> ai-sdk/provider-utils/4.0.46 runtime/bun/…` (`session/llm/request.ts:18,186-201`) | `x-session-affinity`, `x-session-id` |

upshift records only `user-agent`, `x-app` and the `x-stainless-*` family; session-id headers
are deliberately not written to disk, and credential and account-identifier headers
(`x-api-key`, `anthropic-workspace-id`) are recorded as `REDACTED` — present, never valued.

---

## 4. What could not be verified

- **opencode and `ANTHROPIC_BASE_URL`** — opencode exposes no base-URL environment variable
  of its own, so the table names the config key only. Whether `@ai-sdk/anthropic`'s own env
  read still reaches through opencode's provider construction was not observed, and is not
  claimed here.
- **langchain-anthropic reasoning effort** — no such symbol at 0.3.22. Reported as "not
  mapped" rather than mapped to a guess.
- **`ANTHROPIC_BASE_URL` in the vercel-ai-sdk published docs** — verified in source
  (`anthropic-provider.ts:122`); the docs page documents `baseURL` only.
- **The TypeScript SDK's user-agent on the wire** — source-derived, not observed.
- **litellm proxy outbound headers when `forward_client_headers_to_llm_api` is on** — the flag
  was confirmed (`litellm/proxy/litellm_pre_call_utils.py:596`); what it adds was not
  enumerated.
- **The `runtime/…` user-agent segment** for the JS frameworks depends on the host runtime;
  the branching logic was read, the values were not all observed.
