# upshift v1 design

Decisions made by the architect. Subagents and future sessions code against this. Changing a
contract here requires updating this file first.

## Verified external facts (2026-08-27)

- Baseline model API string: `gpt-5.5` (chat/completions + responses).
- Candidate family: `gpt-5.6-sol` (flagship), `gpt-5.6-terra`, `gpt-5.6-luna`. Bare `gpt-5.6`
  is an alias routing to `-sol`; we always use explicit IDs and additionally record the
  resolved model string returned in each API response.
- Documented hard break (LiteLLM #33221, crewAI forum #7648): gpt-5.6-family + function tools
  on `/v1/chat/completions` → 400 "Function tools with reasoning_effort are not supported ...
  use /v1/responses or set reasoning_effort to 'none'". Fires even when reasoning_effort is
  not explicitly set.
- Documented behavioral regressions: duplicated tool calls, acting past the goal, skipped
  tool call + hallucinated confirmation.

## Layout

```
src/upshift/
  schemas.py        # data contracts (this file's source of truth in code)
  checks.py         # deterministic check engine
  agent_loop.py     # generic tool-calling executor, chat_completions + responses
  providers/base.py # Provider interface
  providers/openai_provider.py
  providers/sim.py  # local deterministic simulator of 5.5/5.6 behavior
  runner.py recorder.py stats.py differ.py report.py
  repair/playbook.py repair/loop.py
  patch.py verdict.py cli.py
victim/booking_agent/
  agent.json system_prompt.txt tools.json   # the patchable surface
  backend.py                                # deterministic booking backend
  cases/cases.json                          # 38 eval cases
runs/<run_id>/    # committed to git: run records are the evidence
```

## The victim agent

Flight-booking agent. Tools: `search_flights`, `book_flight`, `cancel_booking`. Backend is a
deterministic in-memory store seeded from each case's `initial_state`; `book_flight` returns
confirmation IDs of the form `UPS-<seq>` deterministically. The agent's entire upgradeable
surface lives in three files (agent.json / system_prompt.txt / tools.json) so every repair is
expressible as a git diff over real files.

`agent.json`: `{name, endpoint: "chat_completions"|"responses", model, params{...},
system_prompt_file, tools_file, max_turns}`. Default endpoint is chat_completions — that is
what breaks on 5.6.

## Eval cases

38 cases in `victim/booking_agent/cases/cases.json`. Mix: happy paths (search/book/cancel),
edge cases (no availability, ambiguous request, already-cancelled, invalid airport, budget
constraints), exact-argument cases (right tool with exact args). Case schema:

```json
{"id": "...", "description": "...", "initial_state": {...}, "user_messages": ["..."],
 "checks": [...], "sim": {"oracle_plan": [...]}}
```

`user_messages` beyond the first are sent sequentially after the agent finishes each turn.
`sim.oracle_plan` is used ONLY by the sim provider (see below); never by checks.

## Checks — deterministic only, no LLM judge in v1

A case passes a rep iff ALL its checks pass. Check types:

- `no_api_error`
- `tool_called {name, args_subset?, exact_args?, min_times?, max_times?}`
- `tool_not_called {name}`
- `final_state {path, equals}` — assertion on backend state (e.g. booking exists/absent)
- `bookings_count {equals}` — total bookings in final state (catches duplicate booking)
- `no_tool_calls_after_success {name}` — over-acting detector: after named tool first
  succeeds, no further tool calls of any kind in later assistant turns
- `confirmation_id_valid` — every `UPS-\d+` mentioned in the final assistant message must
  exist in backend state (catches hallucinated confirmation numbers)
- `response_contains {text}` / `response_not_contains {text}` / `response_matches {regex}`

Rationale: pass/fail must be objective for the statistics to mean anything.

## Runner + recorder

`run_suite(agent_dir, provider, model, overrides, n_reps, run_id)` executes every case
n_reps times. Everything recorded under `runs/<run_id>/`:

- `manifest.json`: run_id, created_at, provider name, resolved agent config, file hashes of
  the three victim files, model requested, n_reps, threshold, upshift version, notes.
- `cases/<case_id>/rep_<k>.json`: every API request and response verbatim, tool executions
  and results, final backend state, per-check results, pass bool, error details, usage,
  latency, `resolved_model` (the `model` field the API returned), seed.
- `summary.json`: per-case pass counts.

Resumable: an existing complete rep file (valid JSON with `pass` key) is skipped on re-run.
Run IDs are explicit, human-supplied or derived (`<model>@<endpoint>-<yyyymmdd>-<n>`), never
random-only. Records are committed to git; they are the evidence behind the verdict.

## Statistics (the load-bearing part)

- Per-case per-model outcome from n_reps: pass_rate ≥ 0.8 → PASS; ≤ 0.4 → FAIL; else FLAKY.
  With default N=5: 4-5/5 PASS, 0-2/5 FAIL, 3/5 FLAKY.
- Case label across (baseline, candidate) outcomes:
  PASS→PASS stable-pass · FAIL→FAIL stable-fail · PASS→FAIL regressed · FAIL→PASS improved ·
  any FLAKY involvement → flaky (annotated with direction, e.g. "flaky (degraded PASS→FLAKY)").
  These are the five labels in the build order; flaky is never silently promoted to
  regressed or improved.
- Every label ships with a one-sided Fisher exact test (hypergeometric, implemented in
  stats.py, no scipy) on pass counts baseline-vs-candidate; p-value shown in the report.
  With N=5, 5/5 vs 0/5 gives p≈0.004; the report shows p so an engineer judges strength.
- Suite-level pass rates get Wilson 95% intervals (implemented in stats.py).
- THE number: restored = regressed cases passing on patched candidate / regressed cases,
  reported together with previously-passing cases broken by the patch (must be 0 to accept).

## Repair loop

Failure signatures drive candidate order (playbook.py):

- `api_error_tools_reasoning` (400 matching the documented 5.6 break) →
  1. endpoint routing: chat_completions → responses
  2. params: reasoning_effort = "none" (stay on chat_completions)
- `duplicate_tool_calls` → prompt edit (execution discipline block), tool schema edit
  (strengthen book_flight description: exactly once per confirmed itinerary)
- `acting_past_goal` → prompt edit (stop-after-goal block)
- `skipped_tool_hallucination` → prompt edit (never state a confirmation number that a tool
  did not return), tool schema edit
- fallback → temperature/reasoning_effort nudges

Candidates are structured `Patch` objects: list of file-level edits to the three victim
files. Loop: apply candidate to a temp copy → screen on previously-regressed cases (N reps)
→ if screen passes, verify on the FULL suite (N reps, candidate model) → accept iff every
originally-regressed case is PASS and no originally-PASS case leaves PASS → else revert, next
candidate. Budget: max 6 candidates. Composable: an accepted candidate becomes the new base
and remaining regressions continue the loop (e.g. endpoint fix first, then prompt fix).

Contested statuses are adjudicated on 2N reps, same thresholds (added 2026-08-28 after the
first real run showed single-sample vetoes firing on borderline-flaky cases — 4/5, 3/5,
3/5, 4/5 for the same case across four verify runs regardless of patch):
- A case only counts as RESTORED if it passes the threshold on screen+verify combined
  (2N reps of the same trial config). Lucky single-run passes do not count.
- A protected or earlier-restored case that dips below PASS in one verify sample is a
  suspect, not a verdict: N adjudication reps of the same config are run and the combined
  2N decides at the same 0.8 threshold. Symmetric evidence, thresholds unchanged — this
  strengthens both directions ("never a single run" applies to vetoes too).
Repair run ids embed a hash of the trial config, so identical re-runs resume from disk
free while a changed candidate lineage gets fresh run dirs.

Patch output: unified git diff over victim files, generated with difflib, applyable with
`git apply`.

## Verdict

- SAFE: zero regressed cases (flaky-degraded reported but does not block; it is listed).
- SAFE WITH PATCH: regressions existed; accepted patch set restores all of them, breaks none.
- STAY PINNED: regressions remain after budget. Report says exactly which cases, with stats.

## Sim provider (honesty rules)

`providers/sim.py` simulates `sim-5.5` and `sim-5.6-sol` so the whole pipeline is testable
deterministically without keys or cost. sim-5.5 executes each case's `sim.oracle_plan`
faithfully. sim-5.6-sol reproduces the documented failure modes: hard 400 on
chat_completions+tools unless reasoning_effort=="none"; seeded corruptions (duplicate the
book call, extra calls after goal, skip tool + hallucinate a confirmation) whose rates drop
when the corresponding repair marker is present in prompt/schema. Seeded per
(case_id, rep, model) — fully deterministic.

Honesty: sim results validate the MACHINERY, not the thesis. Every manifest and report
carries the provider name; the decisive number may only come from provider=openai. The sim's
response to repairs is assumed by construction and proves nothing about real models.

## Batch execution (added 2026-08-27)

The Batch API cannot run an interactive tool loop as one request, but a fleet of episodes
advances in lockstep: every live episode parks its next request, one batch job executes the
wave at 50% token cost, tools run locally, repeat (`providers/openai_batch.py`,
provider name `openai-batch`). Both `/v1/chat/completions` and `/v1/responses` are
batch-supported. Baseline, candidate, and repair screen/verify runs all go through
`run_suite`, so `--batch` covers all of them. Batching changes transport and price, never
request bodies or acceptance criteria; reports treat `openai` and `openai-batch` as equally
real evidence. Exact cost accounting: `upshift cost` sums recorded usage and prices it at
verified rates (`pricing.py`; 5.5: $5/$30, 5.6-sol: $4/$20 per 1M, batch = half).

## Pilot protocol (before the full experiment)

Spend gate: prove a real regression exists for < $10 before funding the full run.
Preferred transport: flex (--flex) — batch-level 50% off, synchronous, and prompt caching
stacks on top; fall back to --batch if flex rejects a model, plain sync as last resort.
Pilot = 8 cases x 5 reps on both models (~80 episodes): 3 duplicate_call-checkable
booking cases, 2 over_acting-checkable, 1 skip_tool-checkable, 1 exact-args search, 1 edge.
After the pilot: report exact token cost + observed regressions, then stop for a go/no-go.
The pilot never changes the final experiment's statistics (N=5, thresholds 0.8/0.4, Fisher
reporting, full-suite verification) — it only de-risks spend.

## Dependencies

Runtime: `openai`, `rich`. Dev: `pytest`, `ruff`. Python ≥3.12. Nothing else without a
reason written here.

Dev-env gotcha (macOS): uv's editable-install `.pth` files under
`.venv/lib/python3.12/site-packages/` can carry the `UF_HIDDEN` flag, and this CPython build
skips hidden `.pth` files, breaking `import upshift`. Fix:
`chflags nohidden .venv/lib/python3.12/site-packages/*.pth` after `uv sync`.

## upshift adapt (v0.2, added 2026-08-31)

One feature: `upshift adapt <path-or-git-url> [--out <dir>]` reads an agent codebase and
emits a complete adapter directory (agent.json, system_prompt.txt, tools.json, backend.py,
cases/cases.json) plus ADAPT_REPORT.md. It collapses the manual adaptation cost (12-15h for
shell_gpt) to minutes of review.

Pipeline (src/upshift/adapt/):
1. inventory.py — walk the repo (git URLs cloned to a temp dir), rank candidate files by
   static signals: openai/litellm imports, chat.completions.create / responses.create /
   litellm.completion call sites, `tools=` kwargs, `"type": "function"` literals,
   prompt-shaped string constants, README/tests/examples. Python files get AST analysis
   (call sites, keyword args, string assignments); other languages fall back to regex.
2. extract.py — the model as extraction engine over the ranked evidence. Input: bounded
   slices (windows around signal lines, top ~15 files, hard token cap). Output:
   schema-validated JSON naming endpoint, model, params, prompt assembly (chunks with
   file:line citations and verbatim|templated|inferred flags), tool schemas (citations),
   tool behavior notes for backend generation, candidate eval scenarios from
   tests/examples/README (citations). Extraction calls go through the normal provider
   machinery (default gpt-5.5, --flex honored) and are RECORDED under runs/adapt-<name>/
   like any run: inputs, outputs, usage — `upshift cost` prices them.
3. verify.py — the anti-hallucination gate, mechanical: every chunk the extraction claims
   as `verbatim` must be a literal substring of the cited file (modulo whitespace
   normalization); failed claims are downgraded to `inferred` and flagged in the report.
   Nothing the model asserts about source is trusted without a citation that checks out.
4. generate.py — write the five files. backend.py: deterministic reimplementation where
   the tool's semantics are mechanical (pure lookups, state machines, file/dir fixtures);
   otherwise a stub returning {"error": "TODO(adapt): not implemented — <what/why>"} that
   satisfies the never-raises contract. Cases: drafts derived from cited usage evidence,
   each with a sim.oracle_plan so the full pipeline runs on --provider sim unchanged.
   Every generated artifact carries provenance comments (file:line).
5. report.py — ADAPT_REPORT.md: found / inferred / undetermined, per-artifact confidence
   (high = verbatim-verified, medium = assembled from cited parts, low = inferred), the
   exact list of lines a human must review before a real run, and extraction cost.

Failure-mode contract: wrong-but-confident is the bug class that kills the feature.
Confidence is derived from the verification gate, never self-reported by the model.
Undetermined beats guessed: a hole with a TODO and a pointer is correct output.

Scope guard: no UI, no new providers, no repair-loop changes, no framework integrations
beyond what extraction must read. No new runtime deps (AST via stdlib).

## Anthropic provider (v0.3, added 2026-09-01)

Second provider, same machinery. Migration under test: `claude-fable-5` (legacy, released
2026-06-09) → `claude-fable-5-1` (released 2026-09-01). Both IDs are pinned snapshots
(docs: "Dateless IDs are their own pinned snapshot"), so no alias drift to guard. Sources:
platform.claude.com/docs/en/models/fable-5-1/{whats-new-fable-5-1,migration-guide},
build-with-claude/prompt-engineering/prompting-claude-fable-5-1, build-with-claude/effort,
agents-and-tools/tool-use/{define-tools,handle-tool-calls}.

Dependency: `anthropic>=1.3` (runtime) — the second provider's transport; nothing else.

### Endpoint `messages` (canonical config → wire)

No provider-specific forks in the core: `agent_loop.py` gains a third endpoint string,
`messages`, next to `chat_completions` and `responses`; providers stay transport-only.

- Request: `{model, max_tokens, system: <prompt string>, messages: [...], tools: [...]}`.
  `max_tokens` is REQUIRED by the API; canonical param `max_tokens` (default 8192 when
  absent — thinking counts against it).
- Tools: canonical `tools.json` stays chat-style; the loop converts each
  `{"type":"function","function":{name,description,parameters}}` to Anthropic
  `{name, description, input_schema: <parameters>}`.
- Params mapping (`map_params`): canonical `reasoning_effort` → `output_config.effort`
  (values low|medium|high|xhigh|max; default high on both Fables; "high" ≡ omitted).
  `tool_choice` passes through in Anthropic shape (`{"type": "auto"|"any"|"none"}` or
  `{"type":"tool","name":...}`); an OpenAI-shaped value is translated (`"required"`→any,
  `{"type":"function","function":{"name":X}}`→tool X). `thinking` passes through (omit ⇒
  adaptive; `enabled`/`disabled` are 400s on both Fables). `temperature/top_p/top_k` pass
  through — non-default values are 400s on BOTH models (not a 5→5.1 regression; see the
  sampling-params detector below).
- Conversation: assistant turns are stored with their FULL `content` block list (thinking +
  text + tool_use) and replayed byte-for-byte; tool results go back as a `user` message
  whose content is `tool_result` blocks FIRST (`tool_use_id`, `content` = JSON-encoded
  backend dict, `is_error` when the dict has "error"), text only after them. Follow-up
  user messages are plain text turns. The loop never edits earlier turns.
- Response parsing: `content[]` blocks → tool calls from `tool_use` (id/name/input), final
  text from `text` blocks; `stop_reason` recorded; `thinking`/`redacted_thinking` blocks kept
  in the recorded assistant turn. Usage: `input_tokens`, `output_tokens`,
  `cache_read_input_tokens` (→ cached_input_tokens), `cache_creation_input_tokens`.
- Provider `anthropic`: `client.messages.create(**request)`, `anthropic-version` handled by
  the SDK, key from `ANTHROPIC_API_KEY` (also via `.env`), records the request AS SENT,
  maps `anthropic.APIStatusError` → ProviderAPIError(status, message verbatim). No flex
  tier exists; no batch provider in v0.3 (ROADMAP).
- Pricing: both Fables $10 in / $50 out per MTok; cache-read fraction is per model:
  0.1 for claude-fable-5 ($1/MTok), 0.025 for claude-fable-5-1 ($0.25/MTok); batch 0.5.
  `pricing.py` gets a per-model cached-input fraction table (default stays 0.1).
- Free preflight: `GET /v1/models/{id}` confirms both IDs exist and records
  `capabilities.effort` levels in the manifest.

### Documented 5 → 5.1 changes as detectors + repairs

Signatures (differ.py) and candidates (playbook.py). Prompt-repair sentences are the
documented wording, verbatim from Anthropic's pages; they are appended to the SYSTEM
prompt (the only placement our repair types allow). The docs' stronger per-turn placement
(text after tool_result blocks, or a turn-scoped system message under the
`mid-conversation-system-clear-at-2026-08-21` beta) is a harness change, not an agent-file
edit, so it is out of scope for the repair loop — reports say so when the system-prompt
placement fails to restore.

1. `api_error_forced_tool_choice` — 400 whose message contains
   `tool_choice: type "tool" and "any" are not supported for this model.`
   Repair (one candidate, two edits): remove `tool_choice` from params AND append the
   instruction-based equivalent to the prompt: for `{"type":"tool","name":X}` →
   `Use the \`X\` tool to answer; call it rather than replying in text.`; for `any` →
   `Respond with a tool call rather than text whenever one of the tools applies.`
2. `thinking_block_invalid` — 400 whose message contains `Invalid \`signature\` in
   \`thinking\` block`. No mechanical repair exists within allowed types (the fix is
   runtime history handling: strip the invalidated run or set
   `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` under the
   `thinking-binding-controls-2026-08-01` beta). The loop REFUSES with that pointer.
   Note: the upshift loop is append-only within an episode, so this fires only for agents
   whose own config rebuilds system/tools mid-conversation.
3. `serialized_tool_calls` — behavioral: candidate issues ≤1 tool_use per assistant turn
   where baseline batched ≥2 (per-case mean calls/turn drop) AND a case check fails.
   New deterministic check `turns_at_most {n}` (assistant turns in the episode) lets a
   case assert the efficiency contract; wall time is reported, never asserted.
   Repair: append the documented batching sentence: `First privately list what you need
   next; then request every item that doesn't depend on another's result in this one
   response.`
4. `reduced_retrieval_calls` — a `tool_called {min_times≥1}` check fails on the candidate
   for a tool the case marks `retrieval: true` (or whose name matches
   search|retriev|lookup|query|fetch|find) while it passed on baseline. Repairs, in order:
   (a) raise effort one rung on the endpoint's ladder (messages: low<medium<high<xhigh<max;
   chat/responses: none<low<medium<high), (b) append the documented verification nudge, verbatim from the prompting guide:
   `When a query centers on a name you do not confidently recognize, or recognize from a
   fast-moving area like AI models and developer tools where the landscape shifts within
   months, the name itself is the thing to verify: search before answering, and include the
   name as the user wrote it in at least one query alongside any reformulations. This holds
   even when you have some background on it — partial background is exactly what makes an
   out-of-date answer sound authoritative, so familiarity is not a reason to skip the
   search.`
5. `api_error_unsupported_sampling_params` — 400 mentioning temperature/top_p/top_k.
   Repair: drop those params (both Fables reject non-defaults; this catches agents
   migrating from OpenAI-style configs).
6. Effort calibration is a first-class `model_params` repair: raise-one-rung and
   set-to-`high` candidates exist for behavioral signatures on any endpoint, using the
   endpoint's legal ladder. Lowering effort is never a regression repair.

Signature computation gains an optional baseline view (`failure_signatures(candidate_reps,
baseline_reps=None)`); acceptance logic in loop.py is unchanged.

### Sim

`sim-fable-5` / `sim-fable-5-1` in providers/sim.py speak the `messages` wire shape. 5.1:
forced tool_choice → the exact 400 above; `serialize` corruption (a plan step with k>1
calls is emitted one call per turn) suppressed when the batching sentence is in the system
prompt; `skip_retrieval` corruption (drops calls to `retrieval` tools when effort < xhigh)
suppressed by the verification nudge or effort ≥ xhigh. Sim evidence is never a verdict.

### Budget reality

No flex tier and $50/MTok output (thinking always on) means a full N=5 pipeline on a
6–8 case suite costs roughly $4–7 per agent. Under the phase cap ($8) the plan is: full
pipeline on one or two agents whose candidate run 400s (rejected requests bill nothing),
detection-only runs for the rest, every run labeled as what it is. N and thresholds are
not lowered to fit a budget.
