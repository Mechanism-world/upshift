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
Pilot = 8 cases x 5 reps on both models via batch (~80 episodes): 3 duplicate_call-checkable
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
