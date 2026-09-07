# Provider parameter matrix

What each endpoint accepts, verified against the vendors' own current documentation. This is
the evidence behind `agent_loop.TRANSLATION_TABLE` (DESIGN.md §G): every row of the table
points at a row here, and a row here that says **unverified** is a row the table refuses to
guess about — it forwards the value and lets the API answer, rather than encoding a rule
nobody checked.

**Checked: 2026-09-07.** No API calls were made; this is documentation reading only. Two
redirects to note: `docs.claude.com` → `platform.claude.com` and `platform.openai.com` →
`developers.openai.com`. Both targets are the vendors' own current doc hosts.

Re-check this file whenever a provider ships a model family, and update the date.

## OpenAI — `/v1/chat/completions` and `/v1/responses`

| Parameter | chat/completions | responses | Legal values / notes | Source |
| --- | --- | --- | --- | --- |
| reasoning effort | `reasoning_effort` | `reasoning.effort` | One value set across both: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. Enumerated for the **gpt-5.6 family** (`gpt-5.6-sol`/`gpt-5.6`, `-terra`, `-luna`, `-cyber`). gpt-5.5 documents only its default (`medium`). `gpt-6-astra` "does not support `none` reasoning effort" and returns HTTP 400 for it. Per-model gaps are real: gpt-5.6-luna answers `Unsupported value: 'minimal' is not supported with the 'gpt-5.6-luna' model. Supported values are: 'none', 'low', 'medium', 'high', 'xhigh', and 'max'.` (rescue-ops evidence). | [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning), [models](https://developers.openai.com/api/docs/models), [changelog](https://developers.openai.com/api/docs/changelog) |
| `temperature`, `top_p` | accepted (top level) | **unverified** — the Responses parameter table could not be fetched in full | Documented rejection is family-specific and thin: "GPT-6 Astra does not support custom `temperature` or `top_p` values or log probabilities". The docs do **not** say whether a request errors or is ignored. Observed in practice: `Unsupported parameter: 'temperature' is not supported with this model.` and `Unsupported value: 'temperature' does not support 0.0 with this model. Only the default (1) value is supported.` (rescue-ops evidence). upshift therefore **forwards them and records a note** rather than dropping or predicting. | [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning), [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create) |
| `top_k` | not an OpenAI parameter | not an OpenAI parameter | Forwarded as declared; the API answers. | — |
| output cap | `max_completion_tokens`; `max_tokens` is "now deprecated in favor of `max_completion_tokens`, and is not compatible with o-series models" | `max_output_tokens` ("including reasoning tokens") | `max_output_tokens` does not exist on chat; the chat spellings do not exist on responses (the SDK raises `TypeError` locally before sending). | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create), [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context) |
| `tool_choice` | `"none"` / `"auto"` / `"required"`; forced function is **nested**: `{"type":"function","function":{"name":...}}`; also `allowed_tools` | `"auto"` (default) / `"required"` / `"none"`; forced function is **flat**: `{"type":"function","name":...}`; also `allowed_tools` | The nested↔flat difference is why endpoint routing needs a translation and not a copy. | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create), [function calling](https://developers.openai.com/api/docs/guides/function-calling) |
| `seed` | documented | **unverified** — not found on any Responses page that could be fetched | Guarantee, verbatim: "our system will make a best effort to sample deterministically, such that repeated requests with the same `seed` and parameters should return the same result. Determinism is not guaranteed." upshift records `determinism: "best_effort"` and never claims a seeded run is reproducible. Forwarded on responses too, because dropping a param the endpoint may accept is worse than letting the API answer. | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create) |
| `store` | both (chat: store output for distillation/evals) | both (responses are "saved for 30 days by default"; disable with `store: false`) | Dropped from agent params by upshift; `build_request` pins `store: false` on responses. | [conversation state](https://developers.openai.com/api/docs/guides/conversation-state) |
| `previous_response_id` | — | responses only: "lets you chain responses and create a threaded conversation" | Dropped: a one-shot replay must not import turns upshift never rendered. | [conversation state](https://developers.openai.com/api/docs/guides/conversation-state) |
| `conversation` | — | responses only; its items "are prepended to `input_items`" | Dropped, same reason. | [responses reference](https://developers.openai.com/api/docs/api-reference/responses/create) |
| `prompt_cache_key` | documented ("Replaces the `user` field") | **unverified** on responses | upshift's own: the OpenAI provider injects a deterministic value for cache-hit routing. Never dropped. | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create) |
| `metadata`, `user` | documented on chat (`metadata`: 16 pairs, keys ≤64, values ≤512; `user`: "stable identifier for your end-users", being replaced by `safety_identifier` and `prompt_cache_key`) | **unverified** on responses | Dropped: an end-user identity is not part of the request contract under test. | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create) |
| `parallel_tool_calls` | documented (boolean) | **unverified** on responses | Not in the translation table: forwarded verbatim and listed in `passthrough_params`. | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create) |
| `service_tier` | `auto`/`default`/`flex`/`scale`/`priority`/`fast` | **unverified** on responses | Injected by the flex provider; forwarded otherwise. | [chat reference](https://developers.openai.com/api/docs/api-reference/chat/create) |
| function tools + reasoning | **rejected**: "Starting with GPT-5.4, Chat Completions does not support tool calling with `reasoning_effort` values other than `none`." "GPT-6 Astra supports Chat Completions, but tool calling requires Responses." | supported | This is the break `route-to-responses` repairs. The live 400 reads `Function tools with reasoning_effort are not supported for <model> in /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'.` — and the second half of that sentence is why `reasoning-effort-none` carries a `changes_capability` disclosure and is ranked last. | [migrate to responses](https://developers.openai.com/api/docs/guides/migrate-to-responses), [latest model](https://developers.openai.com/api/docs/guides/latest-model) |

## Anthropic — Messages API (`/v1/messages`)

| Parameter | Spelling | Legal values / notes | Source |
| --- | --- | --- | --- |
| reasoning effort | `output_config.effort` (top level, no beta header) | `low`, `medium`, `high`, `xhigh`, `max`. **There is no `none` and no `minimal` rung**, and `reasoning_effort` is not an Anthropic spelling at all. Default is `high` on Fable 5.1 / Opus 5 / Sonnet 5, and `"high"` is identical to omitting the param. `xhigh` is narrower than `max` ("Not every model that supports `max` supports `xhigh`"). Haiku 4.5: not supported. Do not pass `adaptive` as an effort value. Per-message effort is beta (`mid-conversation-output-config-2026-07-01`); a model without it answers `output_config.effort requires a model that supports per-turn effort`. | [messages reference](https://platform.claude.com/docs/en/api/messages), [effort](https://platform.claude.com/docs/en/build-with-claude/effort) |
| `temperature`, `top_p`, `top_k` | top level (see below) | Deprecated: "Models released after Claude Opus 4.6 do not support these". Fable 5.1 is explicit: "Non-default `temperature`, `top_p`, or `top_k` values return a 400 error." `top_p` ≥ 0.99 "will be accepted for backwards compatibility, all other values will be rejected with a 400 error." `anthropic` ≥ 1.1.0 removed all three from `Messages.create()`, so upshift routes them through `extra_body` — the SDK's documented escape hatch — and the API answers instead of the SDK. | [messages reference](https://platform.claude.com/docs/en/api/messages), [Fable 5.1 what's new](https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1) |
| `max_tokens` | required | "The maximum number of tokens to generate before stopping." Minimum 0; "Set to `0` to populate the prompt cache without generating a response." | [messages reference](https://platform.claude.com/docs/en/api/messages) |
| `tool_choice` | `{"type":"auto"}`, `{"type":"any"}`, `{"type":"tool","name":...}`, `{"type":"none"}` — `none` **is** supported | Fable 5.1 / Mythos 5.1 "don't support forced tool use": `any` and `tool` return 400 `invalid_request_error` with `tool_choice: type "tool" and "any" are not supported for this model.` (the same validation applies on token counting). `auto` and `none` are unchanged. Restrictions on Opus 5 / Sonnet 5 are **unverified**. | [messages reference](https://platform.claude.com/docs/en/api/messages), [Fable 5.1 what's new](https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1) |
| `seed` | does not exist | Dropped by the translation table, with the reason recorded. | [messages reference](https://platform.claude.com/docs/en/api/messages) |
| model ids | `claude-fable-5-1` ($10/$50, 1M ctx, 128K out, cache read $0.25), `claude-mythos-5-1` (Glasswing only), `claude-opus-5` ($5/$25), `claude-sonnet-5` ($2/$10), `claude-haiku-4-5-20251001` (alias `claude-haiku-4-5`, $1/$5, 200K ctx). Still available: `claude-fable-5`, `claude-mythos-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-opus-4-5-20251101`, `claude-sonnet-4-6`, `claude-sonnet-4-5`. Bedrock prefixes ids with `anthropic.`; Vertex uses e.g. `claude-haiku-4-5@20251001`. | | [models overview](https://platform.claude.com/docs/en/about-claude/models/overview), [Fable 5.1 overview](https://platform.claude.com/docs/en/models/fable-5-1/overview) |

## Unverified

Listed so nobody mistakes an absence for a check. Where a row above is unverified, upshift
forwards the value and records a note; it does not encode a rule.

- **Per-family effort value lists for gpt-5, gpt-5.1, gpt-5.2, gpt-5.5.** The models page
  enumerates only the gpt-5.6 family and gpt-6-astra. Do not assume the seven values apply
  uniformly to older families.
- **Whether gpt-5.x rejects `temperature`/`top_p`, and error vs. ignore.** Only gpt-6-astra is
  documented as not supporting them, and the wording never states the HTTP behaviour.
- **The full `/v1/responses` parameter table.** Both
  `developers.openai.com/api/docs/api-reference/responses/create` and the SDK reference page
  returned content truncated before the parameter definitions. So on Responses specifically:
  `seed`, `temperature`, `top_p`, `prompt_cache_key`, `parallel_tool_calls`, `service_tier`,
  `metadata`, `user`/`safety_identifier` are unverified, as is the exact `reasoning.effort`
  enum as declared there. The Responses column above is inferred from the Chat Completions
  reference and the conversation-state guide.
- **Whether `seed` exists on `/v1/responses` at all.**
- `platform.openai.com/docs/api-reference/chat/create` — could not fetch (HTTP 403); the
  equivalent content came from the developers.openai.com redirect target.
- `developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create`
  — could not fetch (HTTP 404).
- **Opus 5 / Sonnet 5 / Haiku 4.5 sampling-parameter behaviour.** The "after Claude Opus 4.6"
  rule implies rejection, but an explicit 400 was verified only on the Fable 5.1 page.
- **`tool_choice` restrictions on Opus 5 / Sonnet 5.** Only the Fable 5.1 / Mythos 5.1
  restriction is verified.
