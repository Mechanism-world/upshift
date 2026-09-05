# A-032 — PolicyEngine/policybench, `claude-fable-5` → `claude-fable-5-1`

**Terminal state: `REPAIRED_VERIFIED`. Verdict: SAFE WITH PATCH.** The migration hard-fails on
the request policybench sends today; dropping the forced `tool_choice` restores all six
regressed cases with zero collateral, verified on the full suite.

| | |
|---|---|
| repo | https://github.com/PolicyEngine/policybench |
| commit | `ad1583fe6deb6ee70a8b542894edac043b9f4c1e` |
| issue | https://github.com/PolicyEngine/policybench/issues/139 |
| failing call | `policybench/eval_no_tools.py:727-741`; Messages translation `policybench/batch_eval.py:180-226` |
| pair | `claude-fable-5` → `claude-fable-5-1`, endpoint `messages`, provider `anthropic` |
| N | 5 reps per case per model, thresholds pass ≥ 0.8 / fail ≤ 0.4 (never lowered) |
| suite | 7 cases, `agent/cases/cases.json` |
| spend | **$3.5308** of the $5.00 per-case cap |
| upshift | 0.3.1 in the container; reports re-rendered with the v0.3.2-dev fix (§7) |

> **Paths in this published copy.** This report was written against the private lab case
> directory, so bare paths below are relative to it. In this repository they are:
> `agent/*` → `agents/policybench/*`, `report/policybench-tool-choice-auto.patch` →
> `reports/policybench-tool-choice-auto.patch`, `state.json` → `runs/a-032/state.json`;
> `runs/*` paths are unchanged. `cost.json`, `logs/`, `workspace/` and `install_review.txt`
> are lab-internal and are not published here.

---

## 1. What broke

`claude-fable-5-1` rejects the request `policybench` sends. All 35 candidate reps ended in the
same 400 before the model produced anything (`runs/a-032-candidate/cases/*/rep_*.json`, field
`api_error`, verbatim):

```json
{"message": "tool_choice: type \"tool\" and \"any\" are not supported for this model.",
 "status_code": 400, "type": "api_status_error"}
```

The request that produced it (`runs/a-032-candidate/cases/fsm_scenario_008/rep_01.json`,
`api_calls[0].request`) carries `"tool_choice": {"name": "submit_outputs", "type": "tool"}` —
exactly what `policybench` builds: `eval_no_tools.py:727-741` sets
`{"type": "function", "function": {"name": "submit_outputs"}}` and `batch_eval.py:206-211`
translates that to `{"type": "tool", "name": …}` for the Messages API. Nothing about the shape
was invented by this lab; upshift's built request was verified field-by-field against the
harness's own builder before any money was spent (`agent/ADAPT_EDITS.md`).

**This is not the failure the issue predicted.** Issue #139 and
`sensitivity/claude-thinking-2026-08.md` report a *quality* delta on `claude-opus-5` — 79.8
exact forced vs 85.6 under `tool_choice: "auto"`, no error. On `claude-fable-5-1` the canonical
board condition does not degrade; it does not execute. The board's "always force the tool"
invariant is not a scoring choice on this model, it is a hard incompatibility.

## 2. Before, broken, after

| run | config | cases | reps |
|---|---|---:|---:|
| `a-032-baseline` | `claude-fable-5`, forced `tool_choice` | **6/7 pass** | 29/35 |
| `a-032-candidate` | `claude-fable-5-1`, forced `tool_choice` | **0/7 pass** | 0/35 |
| `a-032-c01-439fea01-screen` | `claude-fable-5-1`, patched (6 regressed cases) | 6/6 pass | 30/30 |
| `a-032-c01-439fea01-verify` | `claude-fable-5-1`, patched (**full suite**) | **7/7 pass** | 35/35 |

Per case:

| case | household | reference | baseline | candidate | patched candidate | p (base vs cand) |
|---|---|---:|---:|---:|---:|---|
| `fsm_scenario_000` | scenario_000 | 0 | 5/5 | 0/5 | 5/5 | 0.00397 |
| `fsm_scenario_001` | scenario_001 | 0 | 5/5 | 0/5 | 5/5 | 0.00397 |
| `fsm_scenario_002` | scenario_002 | 0 | 5/5 | 0/5 | 5/5 | 0.00397 |
| `fsm_scenario_008` | scenario_008 | 1 | 5/5 | 0/5 | 5/5 | 0.00397 |
| `fsm_scenario_038` | scenario_038 | 1 | 5/5 | 0/5 | 5/5 | 0.00397 |
| `fsm_scenario_032` | scenario_032 | 1 | 4/5 | 0/5 | 5/5 | 0.0238 |
| `fsm_scenario_012` | scenario_012 | 1 | **0/5** | 0/5 | **5/5** | 1 (stable-fail) |

Every regressed case carried the signature `api_error_forced_tool_choice`. Machine-readable:
`runs/a-032/diff.json`, `runs/a-032/verdict.json`; rendered `runs/a-032/REPORT.md`.

Two rows deserve their footnote rather than a silent line in a table:

- **`fsm_scenario_012` is a stable-fail, not a regression.** It failed on the *old* model too,
  5 reps out of 5, for an ordinary reason: `claude-fable-5` submitted
  `free_school_meals_eligible = 0` where the repo's committed reference says `1`
  (`runs/a-032-baseline/cases/fsm_scenario_012/rep_01.json`, check detail
  `final_state dollars.free_school_meals_eligible is 0, expected 1`). The repo's own snapshot
  metrics put this output's exact rate for `claude-fable-5` at 0.99 over 100 households, so a
  miss somewhere in seven is expected. It is excluded from the regression count — see §5 for
  what happened to it under the patch, and for why that is an observation and not a claim.
- **`fsm_scenario_032` passed the baseline at exactly the threshold** (4/5 = 0.8). The runbook
  calls for 10-rep adjudication of a status that close to a boundary. It has now been attempted
  twice and never ran: the first attempt aborted on the provider's credit-balance error, the
  second (2026-09-05, after credits were refilled) was refused by the budget gate at the $5.00
  per-case cap. Both cost $0.00, so the case remains unadjudicated (§7). It does not change the
  verdict: the case is 0/5 by hard 400 on the candidate and 5/5 under the patch either way. It
  changes only whether the headline reads "6 of 6 restored" or "5 of 5 restored plus one
  underpowered case restored too". **As it stands the record asserts the first reading, on N=5
  evidence: 6 regressed of 7, 6 restored, 0 broken.** Had the 10 reps come back <8/10 the honest
  reclassification would have been 5 regressed and 5 restored, with `fsm_scenario_032` moved to
  the same footnote as `fsm_scenario_012`; that arm was not run, and neither the alternative
  count nor a "confirmed stable pass" is claimed here.

## 3. Root cause

`claude-fable-5-1` removed support for `tool_choice` of type `tool` and `any` on
`/v1/messages` — the documented Fable 5 → 5.1 change, which upshift's differ classifies as
`api_error_forced_tool_choice`. `policybench` reaches it because its answer contract *is* a
forced tool call: `_answer_contract_for_model` returns `"tool"` for any model without a card
and without a `deepseek/`/`gemini/` prefix (`eval_no_tools.py:679-686`), and the forced choice
at `:727-741` is unconditional apart from the `POLICYBENCH_TOOL_CHOICE=auto` environment escape
hatch added by the very commit this case is pinned to.

The escape hatch is therefore already the fix. It is just off by default and documented as a
sensitivity-run-only setting.

## 4. The repair

upshift's first repair candidate — `[model_params] remove-forced-tool-choice` — was accepted on
full-suite verification (`runs/a-032/verdict.json`, `repair_log`):

```
candidate 1/6: [model_params] remove-forced-tool-choice
  screen: 6/6 broken cases restored — running full verification
  ACCEPTED: restored [000, 001, 002, 008, 032, 038]; 0 previously-passing cases broken;
            0 regressed case(s) remain
```

It drops `params.tool_choice` and states the requirement in the system prompt instead
(`runs/a-032/upgrade.patch`, the only two files the repair loop may touch):

```diff
--- a/agent/agent.json
   "params": {
-    "max_tokens": 16384,
-    "tool_choice": { "type": "tool", "name": "submit_outputs" }
+    "max_tokens": 16384
   },
--- a/agent/system_prompt.txt
+Use the `submit_outputs` tool to answer; call it rather than replying in text.
```

**Collateral: none.** The verification run is the *whole* suite at N=5, not just the repaired
cases: 7/7 cases, 35/35 reps. No previously-passing case broke.

The equivalent change to the target's own source is
`report/policybench-tool-choice-auto.patch`, a `git apply`-able diff against the pinned commit
in the file's own style: a `FORCED_TOOL_CHOICE_UNSUPPORTED_MODELS` frozenset beside
`GRANDFATHERED_CHUNKED_CLAUDE_MODELS`, and a two-line guard at the forced-choice site. It
carries the repo's own warning that scores obtained under `auto` are not comparable to the
frozen board. **Its behaviour is what the verified upshift patch demonstrates; the source diff
itself has not been executed in policybench's own harness.**

## 5. The thing we saw that we are not claiming

`fsm_scenario_012` — the household `claude-fable-5` got wrong 5 times out of 5 — came back
5/5 correct in the patched verification run. That is the shape of the owner's own finding
(forcing the tool suppresses Claude's thinking; under `auto` it reasons and scores better).

It is **not evidence for that** here, for two reasons, and the case record should not be read
as if it were:

1. **Two variables changed at once.** The patched run differs from the baseline in both the
   model (`5` → `5.1`) and the tool-choice mode (forced → auto). Nothing in this record
   separates them. Isolating the effect needs a fable-5 + `auto` arm, which was not run.
2. **It is one case at N=5, unadjudicated.** The runbook's answer to exactly this kind of
   single-sample flip is 10 reps at unchanged thresholds. That run could not be made (§7).

What can be said without qualification is narrower and still useful: **under `tool_choice:
"auto"`, `claude-fable-5-1` called `submit_outputs` on all 35 of 35 reps.** The `tool_called`
check with `min_times = max_times = 1` passed in every rep of the verification run, so on this
suite the model never declined the tool and never called it twice. That is the operational
risk of moving to `auto` — the model might just answer in prose — and on these seven households
it did not materialise. It matches the owner's own count under `auto` on `claude-opus-5`:
1,984 tool calls out of 1,984 responses (`sensitivity/claude-thinking-2026-08.md`).

## 6. How the suite was built

Detail in `agent/ADAPT_EDITS.md`; the short version:

- Every prompt and the whole tool schema are byte-for-byte the output of policybench's own
  `_chat_completion_request_kwargs` at the pinned commit, executed inside the lab container
  (`home/gen/gen_requests.py` → `home/gen/requests.json`). `upshift adapt` was not used and no
  prompt text was written by hand. upshift's built request was then compared field-by-field
  against `batch_eval.py`'s own Messages translation: `max_tokens`, `messages`, `model`,
  `tool_choice` and `tools` all match, and no system block is sent because the harness sends
  none.
- One case = one (household, output) pair, because `claude-fable-5` is in
  `GRANDFATHERED_CHUNKED_CLAUDE_MODELS` and really does get one output per request.
- The output is `free_school_meals_eligible`: the highest committed `exact` rate for
  `claude-fable-5` (0.99, n=100, coverage 1.0, in the repo's own
  `paper/snapshot/20260501/runs/…/analysis/metrics.csv`) that still has a non-degenerate mix of
  reference values. Households are the four lowest-numbered with reference 1 and the three
  lowest-numbered with reference 0, so a constant responder scores at most 4/7.
- Expected values come from `paper/snapshot/20260501/us_reference_outputs.csv`, the repo's
  committed PolicyEngine 4.16.1 reference outputs. Checks are deterministic only
  (`no_api_error`, `tool_called` exactly once, `final_state` equality). No LLM judge.
- `agent/cases/cases.json` and `agent/backend.py` were never edited after the baseline run.

## 7. Limitations

1. **`fsm_scenario_032` is unadjudicated — attempted twice, run zero times, $0.00 billed.**
   Its baseline 4/5 sits exactly on the pass threshold and LAB_RUNBOOK §2.5 calls for 10 reps.

   - *First attempt (2026-09-04).* Started — `runs/a-032-adj-baseline`, network probes passed —
     and the provider aborted it before recording anything: *"Your credit balance is too low to
     access the Anthropic API."* The empty run directory is kept as evidence of the attempt.
   - *Second attempt (2026-09-05), after Anthropic credits were refilled.* Refused by the budget
     gate before any call was made, verbatim:

     ```
     $ tools/budget.py check --estimate 1.50 --case-id a-032
     spent $4.3587 / $150.00 · remaining $145.6413 · worst-case estimate for this run $1.5000
     REFUSE: case A-032 has spent $3.5308; +$1.5000 exceeds the $5.00 per-case cap.
     ```

     The blocker this time is **this lab's own per-case cap, not the provider and not the track
     cap**: $3.5308 recorded leaves $1.4692 of headroom against a $1.5000 estimate, while the
     track still has $145.6413. No credit-balance error was seen on this attempt. The estimate
     was not shaved to squeeze under the cap — `budget.py` exists so that a paid run which
     cannot afford to finish never starts, and trimming the number to pass its own gate would
     be the same move as lowering N. The adjudication is therefore `COST_BLOCKED`; the case's
     `REPAIRED_VERIFIED` / SAFE WITH PATCH result does not depend on it (§2), because the patch
     was verified on the full suite at 7/7 cases and 35/35 reps.
   - The same gate blocks the record-only 10-rep re-run of `fsm_scenario_012` under the patch
     (§5). It was not run either, and §5's "observation, not a finding" stands unchanged.

   Recomputed counts: **unchanged** — 6 regressed of 7, 6 restored, 0 broken, 1 stable-fail,
   $3.5308 spent.
2. **The `fsm_scenario_012` improvement is an observation, not a finding.** §5.
3. **The source-level patch is untested in policybench's own harness.** §4.
4. **Scope.** One of policybench's 18 outputs, 7 of its 100 households, one of its 29 board
   models. This is evidence about the request shape the harness sends and about that output —
   not about the leaderboard, and not a measurement of answer quality under `auto`.
5. **Grading tolerance.** policybench scores within $1 of the reference; upshift 0.3.1 has no
   numeric-tolerance check, so `backend.py` rounds the submitted value to the nearest whole
   unit and the case asserts equality. For a 1/0 eligibility output the two rules coincide
   exactly; on a currency output they would not.
6. **Prompt caching.** upshift marks the tools array cacheable (LAB_RUNBOOK §2.1), policybench
   does not. Price differs, inputs do not.
7. **Single turn.** `max_turns: 1`, matching policybench's single-shot read of the first
   response. Any multi-turn behaviour of these models is out of view here.
8. **`claude-fable-5-1` is not on policybench's board.** `config.MODELS` lists `claude-fable-5`
   and not `claude-fable-5-1`; this case answers "what happens when they add it", which is the
   migration question, not a report about a run they have made.
9. **The repair pass was interrupted by an accounting gate, not by evidence.** The case sat at
   `COST_BLOCKED` for one interval because `budget.py`'s unknown-rate guard fired on case
   `A-004`'s unpriced `claude-sonnet-4-5` runs while $148 of the track cap remained. Once that
   model was priced the case resumed from disk on the same `--tag`; no baseline or candidate
   rep was re-billed. Two tooling notes from that episode are in `state.json` under
   `lab_notes` — including that `budget.py check --case-id a-032` then reported $0.00 for
   this case while `--case-id A-032` reported the true figure, because the per-case lookup was
   case-sensitive while `lab_case.py` requires a lowercase case id. **That one is fixed:**
   `budget.py` now folds ids through `norm_case_id`, and as of the 2026-09-05 adjudication
   attempt both casings report $3.5308 and refuse identically.
10. **A product bug was found and fixed mid-case.** upshift 0.3.1's `report._providers`
    recognised only OpenAI providers as real, so this live Anthropic run was stamped
    *"SIMULATED PROVIDER — machinery validation only, not evidence about real models"* — the
    exact inversion of the honesty rule. Failing tests first, then the fix; originally
    `7370cf5` on `fix/anthropic-runs-marked-simulated`, now landed in the Anthropic worktree
    (`5acba1b`, released in `f02ab9a`). `runs/a-032/REPORT.md` is re-rendered from the
    unchanged `diff.json`; the container's v0.3.1 output is preserved verbatim at
    `runs/a-032/REPORT.container-v0.3.1.md`. No number changed, only the provenance line.

## 8. Not done

A pull request against `PolicyEngine/policybench` and a comment on issue #139 are prepared for
the founder to send; as of this report neither has been posted, and nothing else has been posted
anywhere. The source patch (`report/policybench-tool-choice-auto.patch`) and the two texts ship
with the package.
