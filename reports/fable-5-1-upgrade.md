# Release-day report: open-source Claude agents on Claude Fable 5 → Claude Fable 5.1

Claude Fable 5.1 shipped on 2026-09-01. Anthropic documents three breaking changes and
several behavior shifts relative to Claude Fable 5. This report runs real open-source
Claude tool-calling agents through the migration with upshift's statistical pipeline —
N=5 reps per case per model, deterministic checks, Fisher exact p on every label, repairs
verified on the full suite — and says per agent: what broke, how, what a patch restored,
what stayed broken, and the verdict. Every claim is backed by committed run records under
[`runs/`](../runs/) (prefix `fable-`), with error text quoted verbatim from those records.

## What Anthropic documents (the detectors we encoded)

Quoted from the [what's new](https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1)
and [migration guide](https://platform.claude.com/docs/en/models/fable-5-1/migration-guide) pages:

- **Forced tool use returns an error.** `tool_choice` `{"type":"any"}` or
  `{"type":"tool","name":...}` → HTTP 400 `tool_choice: type "tool" and "any" are not
  supported for this model.` — because "Thinking is always on for these models, and a
  forced tool call would skip it." Documented workaround: keep `auto` and state in the
  prompt when the tool applies. upshift: signature `api_error_forced_tool_choice`, repair
  `remove-forced-tool-choice` (removes the param, appends the instruction-based
  equivalent).
- **Thinking blocks bind to the conversation.** Editing anything before a 5.1 thinking
  block invalidates it: 400 ``Invalid `signature` in `thinking` block…``. Earlier models
  can't read 5.1 thinking blocks. upshift: signature `thinking_block_invalid`; no
  agent-file repair exists, so the loop refuses with the documented pointer (strip the
  invalidated run, or `prefix_mismatch_behavior: "drop_block"` under the
  `thinking-binding-controls-2026-08-01` beta).
- **Parallel tool calling is more variable** — "Claude Fable 5.1 may issue one tool call
  per turn where Claude Fable 5 batched several." upshift: `turns_at_most` check,
  signature `serialized_tool_calls`, repair `prompt-batch-tool-calls` with the documented
  sentence verbatim.
- **Fewer search and retrieval calls at low effort.** upshift: signature
  `reduced_retrieval_calls`, repairs `raise-effort-one-rung` then
  `prompt-verification-nudge` (documented paragraph verbatim).
- Also encoded, not a 5→5.1 change: non-default `temperature`/`top_p`/`top_k` are 400s on
  both Fables (`api_error_unsupported_sampling_params`, repair `drop-sampling-params`) —
  it catches agents carrying OpenAI-style sampling params into a Claude config.

## The agents

Selected from a source-verified survey of open-source Claude tool-calling agents
(criteria: calls the Messages API directly with tools, real users, simple harness, tools
honestly determinizable, permissive license, exposure to at least one documented 5.1
change). Adaptation used `upshift adapt` first; every human edit after that is counted in
each agent's `ADAPT_EDITS.md`.

| agent | upstream | exposure | adapt wall clock | human edits after adapt | run tier |
|---|---|---|---|---|---|
| [`agents/fact`](../agents/fact/) | [ruvnet/FACT](https://github.com/ruvnet/FACT) @ b0e3435 (MIT) | `tool_choice: {"type":"any"}` on every call; retrieval-shaped SQL tools | 100s | 3 edits + backend + cases rewritten (binary sqlite fixture unreadable to adapt) | pilot (2 cases × 5 reps) + candidate; stopped on a stable failure |
| [`agents/cookbook-sms`](../agents/cookbook-sms/) | [anthropics/claude-cookbooks](https://github.com/anthropics/claude-cookbooks) tool_use/tool_choice.ipynb @ bbfab1b (MIT) | `tool_choice: {"type":"any"}` SMS bot | 107s, nothing usable (notebooks were unreadable); after the fix: 53s, tools and prompt at high confidence, `tool_choice: any` captured, 4 cases from the notebook cells | hand-adapted by script from the cells before the fix; the post-fix adapt output matches it | full pipeline |
| [`agents/quickstart-agent`](../agents/quickstart-agent/) | [anthropics/claude-quickstarts](https://github.com/anthropics/claude-quickstarts) agents/ @ 3313e97 (MIT) | loop executes every tool_use block of a turn in parallel (`asyncio.gather`) | 83s | 19 edits over 5 files (prompt from the demo notebook, calculator schema from FastMCP, backend + 6 cases written) | detection-only (baseline + candidate) |
| [`agents/claudette-orders`](../agents/claudette-orders/) | [AnswerDotAI/claudette](https://github.com/AnswerDotAI/claudette) toolloop example @ f157e1d (Apache-2.0) | persisted multi-turn history with full assistant blocks; multi-call turns | 110s | 15 edits over 5 files (schemas derived by running claudette's own `get_schema`; backend + cases written) | not run live (sim only) |

Not run: ShenSeanChen/waku-agent (the best retrieval-frequency target; dynamic system
prompt + MCP made honest adaptation the most expensive of the five, and the budget cap
came first).

## Budget and run tiers

Fable models cost $10 / $50 per MTok with thinking always on and no flex tier; this phase
was capped at $8 of Anthropic spend. So: **full pipeline** (baseline → candidate → repair
→ verdict) on the agents whose candidate run consists of rejected requests (which bill
nothing), and **detection-only** (baseline + candidate, full statistics, no repair) where
the candidate run is billed. N=5 and the thresholds were not lowered to fit the budget;
fewer agents got the full pipeline instead.

## Results

### FACT — the migration turns a silent failure into a loud one

Pilot on Fable 5, 2 cases × 5 reps (`runs/fable-fact-baseline/`): **0/5 and 0/5.** Not
because the model was wrong — because with `tool_choice: {"type":"any"}` sent on every
call, it is never allowed to stop. Each response is forced to contain a tool call, so
Fable 5 ran six redundant SQL queries per episode (`SELECT * FROM companies WHERE sector =
'Technology'`, then narrower and narrower variants), hit the turn cap, and produced no text
at all — `stop_reason: tool_use` on the final call, zero text blocks. FACT's own driver has
the same shape (loop up to five iterations while tool blocks exist, then extract text
blocks that, under `any`, never exist, then fall back to an apology string). So as
configured upstream, **FACT cannot answer a question on Fable 5 either**; input tokens
per call grew 1,123 → 3,301 across the six forced turns, and ten episodes cost $1.99.

On Fable 5.1 the same request is rejected outright with the documented
`tool_choice: type "tool" and "any" are not supported for this model.` (`runs/fable-fact-candidate/`: 10/10 reps, that exact text, $0.00 billed; the diff
`runs/diffs/fable-fact-baseline__fable-fact-candidate.md` labels both cases stable-fail,
p=1, signature `api_error_forced_tool_choice`). The honest verdict
is not an upgrade decision: the agent is broken before and after, and the migration
changes the failure from silent (apology fallback after a paid loop) to loud (a 400 that
bills nothing). upshift's `remove-forced-tool-choice` repair — drop the param, tell the
model in the prompt when to use the tools — is the fix on *both* versions; that it is
also the documented 5.1 migration step is the point. We stopped FACT here rather than
spend the remaining budget confirming a stable failure across five more cases.

### Cookbook SMS bot — SAFE WITH PATCH, 5/5 restored, 0 broken

The notebook's SMS bot forces `tool_choice: {"type":"any"}` so the model always replies
through a tool, one call per incoming message. Adapted to mirror that exactly (one API
call per user message; the tool call is the reply).

- **Baseline, Fable 5:** 25/25 reps, 5/5 cases PASS (`runs/fable-sms-baseline/`).
- **Candidate, Fable 5.1, unpatched:** 0/25 — every rep the documented 400
  `tool_choice: type "tool" and "any" are not supported for this model.`; 5/5 cases
  labeled regressed, Fisher p = 0.004 each, signature `api_error_forced_tool_choice`;
  $0.00 billed (`runs/fable-sms-candidate/`).
- **Repair 1 — `remove-forced-tool-choice`** (drop the param, append the documented
  instruction-based equivalent: "Respond with a tool call rather than text whenever one
  of the tools applies."): screen restored 4/5; full-suite verify 5/5 on those four at
  5/5 each. The fifth, `order_help_asks_for_username`, sat at 2/5 on screen and 4/5 on
  verify — 6/10 combined, below the 0.8 bar, so it was *not* counted as restored.
  Accepted: 4 restored, 0 broken.
- **Repairs 2–4** (two prompt blocks, `reasoning_effort: high`): rejected at screen
  (1/5, 1/5, 2/5) — no full-suite spend.
- **Repair 5 — `raise-effort-one-rung`** (`xhigh`): screen 5/5, full-suite verify
  25/25 (10/10 combined on the contested case). Accepted: the fifth case restored.

**Verdict: SAFE WITH PATCH.** Final patch, three lines: `tool_choice` removed,
`reasoning_effort: "xhigh"` added, one instruction sentence appended
(`runs/fable-sms/upgrade.patch`). Cost of the whole pipeline: $0.93. Two honest notes:
the effort bump is what made the "ask for the username" case reliable on 5.1, and
`xhigh` costs more output tokens per call — a tradeoff the patch makes explicit; and
the documented per-turn placement of the instruction (a text block after the tool
results) is stronger than our system-prompt placement, which we do not use.

### Quickstarts agent — no regression detected (detection-only)

The documented shift ("may issue one tool call per turn where Fable 5 batched several")
was the reason to run this agent. Four batching cases plus one smoke case, 5 reps per
model, both models billed (`runs/fable-qs-baseline/`, `runs/fable-qs-candidate/`):

| case | Fable 5 | Fable 5.1 | label |
|---|---|---|---|
| parallel_three_calculations | 5/5 · 3.00 calls/turn | 5/5 · 3.00 | stable-pass |
| parallel_read_three_files | 5/5 · 3.00 | 5/5 · 3.00 | stable-pass |
| parallel_read_two_then_sum | 5/5 · 1.50 | 5/5 · 1.50 | stable-pass |
| parallel_read_three_then_write_report | 5/5 · 2.00 | 5/5 · 2.00 | stable-pass |
| smoke_calculator_two_plus_two | 0/5 | 0/5 | stable-fail |

Fable 5.1 batched exactly as Fable 5 did — identical calls-per-turn on every case — so
`serialized_tool_calls` never fired and there was nothing to repair. That is a negative
result at N=5 on one agent, not a claim that the documented variability doesn't exist.
The smoke case fails on both models for a reason unrelated to the migration: the model
answers "2+2" without a calculator, and the case demanded the tool — a case-authoring
flaw we left in rather than quietly delete. $1.16.

### claudette toolloop example — adapted, sim-validated, not run live

Adapted with schemas derived by running claudette's own `get_schema`; passes the full
sim pipeline. Not run against the real API: its exposure is behavioral only (its toolloop
does not force `tool_choice`), the quickstarts run had just shown no parallelism shift,
and the budget cap came first. Two findings from reading its code, both recorded in
`agents/claudette-orders/ATTRIBUTION.md`: `Chat` sends `temperature=0` unconditionally,
which both Fables reject (a 400 on *either* model, so claudette cannot call a Fable at
all without a code change), and its pricing table lacks Fable ids, so displaying a chat
result raises `KeyError`.

## Summary

| agent | break on 5.1 | patch | verdict | Anthropic spend |
|---|---|---|---|---|
| FACT | forced `any` → 400 (already unable to answer on Fable 5) | not attempted: stable failure | no upgrade decision possible; fix the config first | $1.99 |
| Cookbook SMS bot | forced `any` → 400, 5/5 cases | remove `tool_choice` + instruction, effort → `xhigh` | **SAFE WITH PATCH** 5/5, 0 broken | $0.93 (+$0.85 pre-fix pilot) |
| Quickstarts agent | none detected | — | **SAFE** on the cases run (detection-only) | $1.16 |
| claudette | not run live | — | sim-validated only | $0 |

Upstream issues filed with these records: [ruvnet/FACT#5](https://github.com/ruvnet/FACT/issues/5)
and [anthropics/claude-cookbooks#854](https://github.com/anthropics/claude-cookbooks/issues/854)
(2026-09-02).

Total: **$4.94** of the $8 cap, plus a ~$0.05 unrecorded wire-format smoke test.
Every number above is reproducible from the committed records with `upshift cost` and
`upshift diff`.




## Limits of this report

- Two of the four agents are reference code from Anthropic's own repos (cookbook,
  quickstarts) — widely forked, but "users" there means forks, not deployments.
- Every eval suite is ours, derived from each project's README/notebooks/tests; a
  maintainer might weight behaviors differently.
- Adapted agents run through upshift's loop, not their own harness: no streaming, a turn
  cap, tool results JSON-wrapped. None of these change whether the API accepts a request.
- The thinking-block invalidation break cannot fire inside upshift's append-only loop; it
  is detected and refused, not exercised, here.
