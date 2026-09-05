# A-085 — LayerDynamics/plastiq, `claude-opus-4-8` → `claude-fable-5-1`

**Terminal state: `REPAIRED_VERIFIED` — `SAFE WITH PATCH`.** The migration breaks the agent
completely and a patch restores it, both measured on the real Anthropic API. Baseline **5/5
cases (25/25 reps)** on `claude-opus-4-8`; candidate **0/5 cases (0/25 reps)** on
`claude-fable-5-1`, every rep a 400 on the request body; patched candidate **5/5 cases (25/25
reps)** on full-suite verification. **$1.5495** of the $5.00 per-case cap.

This supersedes the 2026-09-04 record, which stopped at `API_UNAVAILABLE` with nothing
measured (§6). The suite, the adapter, N and the thresholds are byte-identical between the two
attempts — the same `--tag a-085` resumed the run from the manifest already on disk.

| | |
|---|---|
| repo | https://github.com/LayerDynamics/plastiq |
| commit | `7f84779fdf9ca5ce62b0dc71bcbd96547f7ae207` (2026-07-03, *"feat(ai): anthropic tool_choice adapter — forced tool drops adaptive thinking (CB6.2.2)"*) — the commit that **introduces** the forcing adapter |
| source | the commit itself (`codesweep-039`); parent `4614b7a5` has no `tool_choice` mapping at all |
| failing call | `apps/plastiq/src/ai/providers/anthropic.ts:36-41` (`toAnthropicToolChoice`) and `:194-217` (`AnthropicAdapter.stream`, `useThinking = this.thinking && !forcesTool` at `:202`) |
| pair | `claude-opus-4-8` → `claude-fable-5-1`, endpoint `messages`, provider `anthropic` (real, not simulated) |
| N | 5 reps per case per model, thresholds pass ≥ 0.8 / fail ≤ 0.4 (unchanged; never lowered) |
| suite | 5 cases, `agent/cases/cases.json`, every user turn verbatim from the repo's own tests |
| spend | **$1.5495** of the $5.00 per-case cap (`cost.json`, priced from the API's own usage fields) |
| upshift | 0.3.1 |

> **Paths in this published copy.** This report was written against the private lab case
> directory, so bare paths below are relative to it. In this repository they are:
> `agent/*` → `agents/plastiq/*`, `report/plastiq-drop-forced-tool-choice.patch` →
> `reports/plastiq-drop-forced-tool-choice.patch`, `state.json` → `runs/a-085/state.json`;
> `runs/*` paths are unchanged. `cost.json`, `logs/`, `workspace/` and `install_review.txt`
> are lab-internal and are not published here.

## 0. Why this pair, and why the repo curates no Fable

`apps/plastiq/src/ai/providers/models.ts:44-46` curates exactly `claude-opus-4-8`,
`claude-sonnet-4-6` and `claude-haiku-4-5` — **no Fable model**. The reporter names no migration
pair. The in-scope pair is therefore `claude-opus-4-8` → `claude-fable-5-1`: the newest model
the repo actually offers, moving to the model this track is about. This is stated because it is
a choice, not a given — plastiq has not attempted this migration, and this case asks what
happens when it does.

## 1. What broke, and why

plastiq forces a tool on the agent's first turn — the `firstTool` seam
(`agentRunner.ts:70-73`, threaded through `runGeneration.ts:36-37,52`,
`headless/generate.ts:34-35,123`, and `plastiq-gen --first-tool` / `cadbench-harness run
--first-tool`), whose purpose is documented as CB6.2: *"get a real model to call `build_part`
(not `answer_user`)"* (`docs/plans/2026-06-22-cadgenbench-integration.md`). On the Anthropic
path that becomes `tool_choice: {"type":"tool","name":"build_part"}`
(`toAnthropicToolChoice`, `anthropic.ts:36-41`).

The commit under test already knows one Anthropic constraint and mitigates it. The in-code
comment at `:197-200`, verbatim: *"Anthropic rejects extended thinking combined with FORCED tool
use (`any`/`tool` ⇒ 400)"* — so `:202` computes `useThinking = this.thinking && !forcesTool` and
omits the `thinking` block on a forced turn.

**That mitigation is the exact thing `claude-fable-5-1` makes moot, and the run confirms it.**
Every one of the 25 candidate reps failed on the first API call, before any tokens were
generated, with the same error — verbatim from
`runs/a-085-candidate/cases/cube_40mm/rep_01.json`:

```
tool_choice: type "tool" and "any" are not supported for this model.   (status 400)
```

No `thinking` block was in that request; plastiq's own mitigation had already removed it.
Thinking was never the only reason for the rejection. On `claude-fable-5-1` there is no request
plastiq can send that both forces the tool and succeeds, and the repo's own escape hatch does
not exist: `firstTool` is an option, not a setting, and there is no model-conditional branch in
the adapter at this commit.

Signature: `api_error_forced_tool_choice` on all five cases — the same signature and the same
verbatim message this lab observed live on `cases/A-032`, now confirmed on a second target.

## 2. Before and after

```
claude-opus-4-8 @ messages  ->  claude-fable-5-1 @ messages
provider: anthropic    n_reps: 5

baseline   5/5 cases pass (100.0%, Wilson 95% CI 56.6-100.0%)
candidate  0/5 cases pass (  0.0%, Wilson 95% CI  0.0- 43.4%)
5 regressed, 0 flaky, 0 improved
```

| case | baseline `claude-opus-4-8` | candidate `claude-fable-5-1` | patched candidate | label | p |
|---|---|---|---|---|---|
| `cube_40mm` | 5/5 | 0/5 | 5/5 | regressed → restored | 0.00397 |
| `cube_20mm` | 5/5 | 0/5 | 5/5 | regressed → restored | 0.00397 |
| `cube_10mm` | 5/5 | 0/5 | 5/5 | regressed → restored | 0.00397 |
| `box_10x20x30` | 5/5 | 0/5 | 5/5 | regressed → restored | 0.00397 |
| `box_with_hole` | 5/5 | 0/5 | 5/5 | regressed → restored | 0.00397 |
| **total** | **5/5 cases, 25/25 reps** | **0/5 cases, 0/25 reps** | **5/5 cases, 25/25 reps** | | |

p is a one-sided Fisher exact test on baseline vs candidate passes. Every count is a whole
number of reps at the extremes; **no case sits between the thresholds**, on either model or
under the patch.

Runs, all four on the real API, all under tag `a-085`:

| run | model | what | cases / reps | cost |
|---|---|---|---|---|
| `a-085-baseline` | `claude-opus-4-8` | baseline | 5/5 · 25/25 | $0.4867 |
| `a-085-candidate` | `claude-fable-5-1` | candidate, unpatched | 0/5 · 0/25 | $0.0000 |
| `a-085-c01-a6d7471f-screen` | `claude-fable-5-1` | patched, the broken cases | 5/5 · 25/25 | $0.8094 |
| `a-085-c01-a6d7471f-verify` | `claude-fable-5-1` | patched, **full-suite verification** | 5/5 · 25/25 | $0.2535 |

The candidate pass cost **$0.0000**: the 400 is a request-body rejection, so the provider billed
no tokens for any of its 25 reps. The full transcripts of all 100 live reps are under
`runs/<run-id>/cases/<case>/rep_NN.json`.

The `runs/a085-sim/` records from the earlier $0 machinery smoke are retained and are **not**
evidence; the report for the live run carries no simulated-provider stamp, because `anthropic`
is a real provider.

## 3. The suite

Five cases, N=5, deterministic checks only, every user turn copied verbatim from the pinned
repo. Built by hand — `upshift adapt` was not run and $0.00 went to adaptation. Full ledger in
`agent/ADAPT_EDITS.md`; the short version:

| case | user message | source in the repo |
|---|---|---|
| `cube_40mm` | `Make a 40 mm cube.` | `providers/anthropic.integration.test.ts:35` (the repo's own live Anthropic round-trip) |
| `cube_20mm` | `make a 20mm cube` | `runGeneration.unit.test.ts:70`, `aiStore.unit.test.ts:66` |
| `cube_10mm` | `make a 10mm cube` | `persistence/projectsStore.test.ts:221` |
| `box_10x20x30` | `Make a 10×20×30 mm box.` | `headless/generate.test.ts:48` |
| `box_with_hole` | `make a box with a hole` | `tools/toolDefs.unit.test.ts:218` |

Checks: `no_api_error`; `tool_called build_part` exactly once (CB6.2's contract); the first
feature is a `box` (`prompt.ts:30`, *a rectangular block is a `box`*); the box's sorted
dimensions in mm on the four prompts that state dimensions (`prompt.ts:15`, *"every length you
write is in MILLIMETRES"*); and zero structural violations of the document rules the repo
states. No LLM judge.

The request the adapter sends is plastiq's forced turn field-for-field — `max_tokens: 16000`
(`anthropic.ts:183`), the `parametricSystemPrompt()` system string, the four
`toolDefs({creative:false})` tools, `tool_choice: {"type":"tool","name":"build_part"}`, and
**no `thinking` block**, because plastiq's own mitigation omits it on a forced turn. The
episode is capped at one assistant turn, which is the only turn the forcing applies to; §7 lists
that and the other deviations.

Both models resolved to the requested ids (`resolved_model` in every rep record).

## 4. The patch — **VERIFIED**

### 4.1 What was verified

The repair loop accepted its **first** candidate, `[model_params] remove-forced-tool-choice`,
on full-suite verification. `runs/a-085/upgrade.patch`, against the two files the loop is
allowed to write:

```diff
--- a/agent/agent.json
+++ b/agent/agent.json
   "params": {
-    "max_tokens": 16000,
-    "tool_choice": {
-      "type": "tool",
-      "name": "build_part"
-    }
+    "max_tokens": 16000
   },
--- a/agent/system_prompt.txt
+++ b/agent/system_prompt.txt
 Never say you created, edited, or fixed a part unless you called build_part this turn.
+
+Use the `build_part` tool to answer; call it rather than replying in text.
```

That sentence is Anthropic's documented substitute for a `tool_choice` that named one tool
(`upshift/repair/playbook.py:73`, `FORCED_TOOL_INSTRUCTION`), used verbatim.

Acceptance, from `runs/a-085/verdict.json`:

```
candidate 1/6: [model_params] remove-forced-tool-choice
  screen: 5/5 broken cases restored — running full verification
  ACCEPTED: restored [box_10x20x30, box_with_hole, cube_10mm, cube_20mm, cube_40mm];
            0 previously-passing cases broken; 0 regressed case(s) remain
```

Under the patch the model does what the forcing used to compel: `box_10x20x30` rep 01, for
instance, returns a single `build_part` call whose document is
`{features:[{id:"f1", type:"box", params:{dx:10, dy:20, dz:30}}]}` with zero structural
violations. Persuasion held on **25/25 reps across all five cases**; not one rep answered in
prose. That is the empirical question the previous report's §4 could not answer, and it is
answered for this suite at this N.

**Honest limit on "0 collateral damage":** all five cases regressed, so the set of
previously-passing cases *outside* the regression was empty. The full-suite verification
therefore re-ran the same five cases and found them all passing; it had no untouched case to
protect. The claim "0 previously-passing cases broken" is true and is the strongest this suite
can support — it is not evidence that nothing elsewhere in plastiq is affected.

### 4.2 The same repair in plastiq's own code

`report/plastiq-drop-forced-tool-choice.patch` — a `git apply --check`-clean diff against the
pinned commit, +42/−4 lines in `apps/plastiq/src/ai/providers/anthropic.ts`, in that file's own
style. It was **revised after the run to match what was actually verified**; two differences
from the version drafted on 2026-09-04:

1. **The instruction text.** The draft appended the generic sentence *"Respond with a tool call
   rather than text whenever one of the tools applies."* The repair that was actually verified
   appended the **named-tool** sentence, *"Use the `build_part` tool to answer; call it rather
   than replying in text."*, because the dropped choice named a tool. The patch now derives the
   sentence from the tool choice: named tool → the verified sentence; `any` → the generic one,
   which is Anthropic's documented wording for that case but was **not** exercised here.
2. **The thinking block.** The draft let the existing mitigation lapse on the substitute path,
   so `forcesTool` became false and adaptive thinking was sent again on that turn. The verified
   request carries **no `thinking` block** — upshift never sends one. `forcesTool` is now
   computed from the *requested* choice rather than the surviving one, so a turn whose forcing
   was dropped still omits thinking, and the patched request shape is exactly the shape that
   passed 25/25. Re-enabling adaptive thinking there may well work; nothing in this record shows
   that it does, so the patch does not do it.

Still unverified about this file-level patch, stated plainly: the model-id list
(`FORCED_TOOL_CHOICE_UNSUPPORTED = ["claude-fable-5-1"]`) is a hardcoded prefix list, and a
capability probe or a config flag may suit the repo better; the `any` branch is untested; and
the patch was never compiled or run — no TypeScript toolchain was installed for this case
(§"Install" in `agent/ADAPT_EDITS.md`). What is verified is the **behaviour** of the repair —
drop the forced choice, state it in the system prompt — on the real model, through upshift's
port of plastiq's request construction.

A pull request against `LayerDynamics/plastiq` is prepared for the founder to send; as of this
report it has not been opened, and nothing has been posted anywhere. There is no originating
issue for this case, so the pull request is the only external artifact.

## 5. Adjudication, and the product fixes this case needed

**No 2N adjudication was needed.** The runbook requires 10 reps for any contested status: a case
near a threshold, or a restoration or veto that rests on a single run. There is none here — every
case is 5/5 on baseline, 0/5 unpatched and 5/5 patched, the differ reported zero flaky cases, and
each restoration was confirmed twice (screen and full-suite verification) at N=5. Nothing in this
record is decided by a margin of one rep.

The two product prerequisites this case identified on 2026-09-04 both landed on the product
worktree's `main` before the resume, and both were load-bearing:

- **`fix/zero-usage-runs-price-as-zero`** (`de35d1f`, merged as `33d62c3`): `pricing.price()`
  returned `None` ("unknown rate") for a run that recorded **zero** tokens whenever the model had
  no `RATES` entry. This case's aborted, $0, evidence-free run therefore put an unknown-rate
  record on the ledger, and `budget.py check` refuses to authorise any spend on any case while
  one is on record — a run that provably billed nothing froze the whole track. Zero usage costs
  zero at any rate. `tests/test_pricing.py::test_zero_usage_is_zero_even_for_an_unpriced_model`
  asserts both directions.
- **`claude-opus-4-8` pricing** (`8903380`): $5/$25 per MTok, cache reads at the documented
  default 10% ($0.50/MTok). Without it, the completed baseline in this very run would have been
  reported as unknown-rate and would have blocked every later authorisation exactly as `A-032`
  describes. `cost.json` now reads `unknown_rate: false`.

The resumed run found **no new product bug**: upshift 0.3.1 resumed from the aborted manifest,
priced all four runs, and reached a terminal state without a workaround.

## 6. History: why the first attempt recorded nothing

Pass 1 (baseline, `claude-opus-4-8`) aborted on its first API call, 2026-09-04T01:32:30Z,
upshift exit 2. Verbatim (`logs/api-unavailable.txt`, retained; also `state.json`
`prior_attempt_api_unavailable`):

```
Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing
to upgrade or purchase credits.
```

Nothing was recorded — `runs/a-085-baseline/` held a manifest and no rep files, $0.0000 billed —
because a billing 400 is a provider condition, not a model result, and upshift aborts rather
than write a diff over an incomplete run. The account was funded on 2026-09-05 and the run
resumed from that same manifest. All three run-phase network probes matched the specification on
both attempts: `pypi.org` blocked through the proxy (curl 56), `1.1.1.1` unreachable directly
(curl 7, no route), `api.anthropic.com` reachable (HTTP 401 unauthenticated).

## 7. Limitations

1. **Non-streaming.** plastiq's adapter streams (`stream: true`, `anthropic.ts:214`, SSE reducer
   at `:117-159`); upshift does not. The failure under test is a 400 on the request body, where
   transport is irrelevant, but this adapter is a port of plastiq's request construction only
   and says nothing about its SSE path — including whether the substitute instruction behaves
   the same when the response is consumed incrementally.
2. **One turn.** `max_turns: 1` — the forced turn, which is the only turn `firstTool` applies
   to. The self-correction loop, the `answer_user` finalizer and multi-turn behaviour are out of
   view. The patched runs exercise only the *first* turn's substitute instruction, never the
   loop's later `auto` turns.
3. **No CAD kernel.** `backend.py` enforces the schema, the `"assembly"` guard and the
   documented sketch-before-`extrude`/`revolve`/`cut` rule; it does not evaluate geometry. A
   structurally valid but geometrically empty document counts as built here and would not
   upstream. That direction is conservative for a regression question — it can only make a case
   easier to pass, equally on both models.
4. **`tools.json` is a hand transcription** of a schema plastiq generates at runtime from zod,
   in the dereferenced form the repo's own headless path sends (`nodeBuild.ts:31-96`), with
   `additionalProperties` deliberately omitted where zod's exact emission was not verified.
   Detail and reasoning in `agent/ADAPT_EDITS.md` §3.
5. **No in-repo caller wires `firstTool` to the Anthropic adapter at this commit.** The browser
   panels (`GenerationPanel.tsx:547`, `CommandPalette.tsx:107`) do not pass it, and `plastiq-gen`
   builds an OpenAI-compatible provider only (`headless/cli.ts:153-160`). The forced Anthropic
   turn is exercised by the adapter's own unit tests (`anthropic.unit.test.ts:91-100`) and is a
   supported option of `runGeneration`. So this case is about a path the repo built, tested and
   documented in the same commit — not about an outage its users are hitting today.
6. **Five cases, one agent, one tool.** The suite covers the parametric agent's forced first
   turn. It says nothing about the creative `create_mesh` path, the vision/caption pipeline, or
   the OpenAI-compatible adapter. See also §4.1 on what "0 collateral" can and cannot mean when
   every case in the suite regressed.
7. **Prompt caching.** upshift marks the system block and the last tool definition cacheable;
   plastiq does not. Price differs, inputs do not. (Cache reads are folded back into the recorded
   input totals, so `cost.json` is the billable total, not an underestimate.)
8. **N=5, and the thresholds were never moved.** A 25/25 result bounds that arm's true rep pass
   rate at 86.7-100% (Wilson 95%); it is not a claim of perfection at scale.
