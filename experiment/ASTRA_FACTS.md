# GPT-6 Astra — verified migration facts

Fetched 2026-09-14 from official OpenAI developer documentation. Every line below is
sourced. Nothing here is carried over from GPT-5.x assumptions.

## Model identity
- Model id: `gpt-6-astra` (single snapshot; no dated suffix published)
- Context window 1,050,000; max input 922,000; max output 128,000
- Price: $10.00 / MTok input, $50.00 / MTok output; cached input $1.00; cache writes $12.50
- Source: https://developers.openai.com/api/docs/models/gpt-6-astra

## BREAKING — endpoint / tool calling
> "Use the Responses API for function calling. Chat Completions does not support
> function calling with GPT-6 Astra."
- Source: https://developers.openai.com/api/docs/guides/reasoning
- Non-tool Chat Completions requests remain supported.
- Observed wire error when tools are sent to `/v1/chat/completions`:
  `400 Function tools with reasoning_effort are not supported for gpt-6-astra in
   /v1/chat/completions. To use function tools, use /v1/responses or set
   reasoning_effort to 'none'.`

### TRAP (important for scoring)
The 400 message advertises `reasoning_effort: 'none'` as an escape hatch. That escape
hatch is itself invalid on this model (see next section). An engineer or tool that
follows the error message literally produces a second 400. The only correct fix is to
route to `/v1/responses`.

## BREAKING — reasoning effort
> "GPT-6 Astra does not support `none` reasoning effort. Setting `reasoning.effort`
> (Responses) or `reasoning_effort` (Chat Completions) to `none` returns HTTP 400."
- Valid values: `low`, `medium`, `high`, `xhigh`, `max`
- Migration guidance: if previously using `none` or `minimal`, start with `low`.
- Spelling: `reasoning.effort` on Responses; `reasoning_effort` on Chat Completions.
- Source: https://developers.openai.com/api/docs/guides/reasoning
           https://developers.openai.com/api/docs/guides/latest-model

## BREAKING — removed sampling / logprob parameters
Remove: `temperature`, `top_p`, `top_logprobs`.
Chat Completions additionally: remove `logprobs`.
Responses additionally: remove `message.output_text.logprobs` from `include`.
- Source: https://developers.openai.com/api/docs/guides/latest-model

## BREAKING — prompt caching
Replace `prompt_cache_retention` with `prompt_cache_options.ttl` set to `"30m"`.
- Source: https://developers.openai.com/api/docs/guides/latest-model

## BEHAVIORAL changes (not wire errors — these are the hard ones)
Documented differences vs GPT-5.x:
1. Initiative/follow-through: "more likely to ask the user a question when additional
   input could materially change the result." -> risk: an autonomous agent loop that
   expected a tool call or a final answer now receives a clarifying question.
2. Writing style: "tends toward detailed, formatted responses" with lists and tables;
   recurring phrases. -> risk: output-format assertions, parsers, structured
   expectations, and terse-output contracts break.
3. Instruction following: more sensitive to instructions in accessible files
   (`AGENTS.md`, skill files); user instructions should take precedence.
4. Subagent delegation: may delegate less frequently than desired.
5. Testing approach: verifies more thoroughly than necessary on coding tasks.
- Source: https://developers.openai.com/api/docs/guides/latest-model

## Structured outputs
Listed as supported. No documented schema-format change for this model. Any
strict-schema behavior difference must be established empirically, not assumed.

## Not applicable / deliberately unpriced
Fast mode: unavailable with EU data residency, no latency SLA. Long-context (272K+)
and batch/flex tiers carry separate modifiers and are out of scope for this experiment.

## Environment verification (2026-09-14, free endpoint)
`GET /v1/models` with the project key: HTTP 200, 130 models.
`gpt-6-astra` is present and accessible. The full gpt-5.x family is present
(gpt-5.5, gpt-5.5-2026-04-23, gpt-5.6-luna/sol/terra, ...).
No paid call has been made.
