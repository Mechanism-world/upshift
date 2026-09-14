# COMPARISON — post-unblinding

Written by the independent evaluator after `SCORING.md` was final and hashed
(`945ea0ed…96e195`). Unblinding: **Candidate X = Arm B (with Upshift). Candidate Y = Arm A
(Claude Code + docs + the app's own tests).**

Nothing in `SCORING.md` has been changed. This document reinterprets what was already
measured, adds the efficiency comparison, and checks the arms' self-reported claims against
my own instruments and against the product's source.

---

## 0. Bottom line first

**LOSS for the tool on this trial.**

On this migration Upshift consumed ~25 minutes of attended work (≈60 % of the 40 minutes
Arm B's migration actually took) and **$1.42** — roughly **7× Arm B's entire direct API
spend of $0.206** — and returned **one** observation the engineer did not already have,
which changed nothing in the shipped patch. It proposed **0 of 3** repairs that were
accepted, caught **0** collateral regressions, and its headline verdict was **`STAY
PINNED`** — which I have independently measured to be **wrong**: both patches are SUCCESS,
zero regressions across 11 054 tests, M1–M4 passing live at N=5. The error is in the
dangerous direction: a false block on a migration that is in fact safe.

**And my tie-break does not rescue it.** I ranked Candidate X above Candidate Y on a single
cell out of eighteen. That cell rests on the finding that the accepted reasoning-effort set
is endpoint-dependent — which Arm B's own contemporaneous log attributes to **its own probe
script**, classifies as **class A** ("I would have found it from docs/code first"), and
annotates *"Not in the supplied fact sheet, not from Upshift."* Upshift never tested
reasoning effort at all in that run. **The margin is the engineer's, not the tool's.**

---

## 1. What did the extra workflow actually buy?

Measured against the shipped artifacts: **nothing that reached the patch.**

The two patches are ~80 % the same migration (`SCORING.md` §4): the same new `tools`
parameter on `_should_use_responses_api` with the same override semantics, a module-level
table *literally identically named* `_MODELS_REQUIRING_RESPONSES_FOR_TOOLS` containing
exactly `"gpt-6-astra"` in both, the same per-model effort narrowing with SDK-wide
fallback, and — independently, in both — the same non-obvious repair of upstream's three
`startswith("gpt-5")` branches that silently stop matching at GPT-6. Two engineers, no
communication, same two repairs.

Arm B's own ledger of what the tool produced, which my measurements corroborate:

| Upshift output | Arm B's class | My check |
|---|---|---|
| U1 tools require `/v1/responses` | A — logged 19:07, before Upshift was run | I measured this is **already true unpatched**; nothing to find |
| U2 Astra rejects `none`/`minimal` — **the one blocking defect** | A — logged 19:07, confirmed by the engineer's own free 400-probes | Confirmed live by me; Upshift's adapter sent no effort param, so it **could not** have found it |
| U3 effort set is endpoint-dependent | A — engineer's probe script | Confirmed live by me with a direct call; see §2 |
| U4 Astra prefers `shell`+`printf` over the `save` tool | **B — genuinely new** | Real, and **changed nothing**: neither patch addresses tool preference |
| U5 more self-verification turns | C — already in `ASTRA_FACTS.md` | — |
| U6 `temperature`/`top_p` not sent | F | I measured M2 already satisfied unpatched; Upshift's agent carried `params: {}` because the engineer wrote it that way |
| U7 `c3_exact_write` regressed 4/5→1/5 | **false positive** | task completed correctly; the check asserted a tool *by name*. **Upshift's `STAY PINNED` verdict rests on this** |
| U8 `c4_no_overreach` improved 0/5→5/5 | **false positive** | artifact of the hand-written fake backend |

**3 of the 4 signals in the live run were artifacts of the adapter the engineer had written
20 minutes earlier.** The tool measured a reconstruction, and the reconstruction's defects
became the findings.

What it bought, precisely: **one behavioural observation (U4) with no effect on the
artifact**, plus a wrong verdict, plus 25 minutes, plus $1.42.

---

## 2. Is the rung-1 margin attributable to Upshift or to the engineer?

**To the engineer. Unambiguously, and the attribution is checkable three ways.**

1. **Arm B's own classification.** `UPSHIFT_CONTRIBUTION.md` §4 lists U3 as class **A**,
   evidence: *"my probe2 script, run before the live Upshift leg finished. Not in the
   supplied fact sheet, not from Upshift."* This log was written as the work happened,
   specifically to make this distinction possible — and it is written *against* the tool.
2. **Mechanism.** Upshift could not have found it. Arm B's adapter declared no reasoning
   parameters (`params: {}`), so no Upshift request ever carried a reasoning effort on
   either endpoint. A tool that never sends the parameter cannot discover which values it
   accepts.
3. **My own reproduction.** I found the same fact, blind, by the same method: one direct
   `POST /v1/chat/completions` with `reasoning_effort: "max"`, which returned
   `Unsupported value: 'reasoning_effort' does not support 'max' with this model.
   Supported values are: 'low', 'medium', 'high', and 'xhigh'.` That call cost **$0** — a
   rejected request bills nothing. It took me under a minute and required no tooling.

So the one dimension on which the Upshift arm's patch is better is a dimension the tool did
not touch. Crediting it to Upshift would be a plain attribution error, and the product's
own arm declined to make it.

**On the weight of my tie-break itself.** I am asked whether an 18-cell grid decided by one
cell is a basis for calling a winner. As a statement about the two *patches*, the ranking
stands: X does not emit a request the model refuses in any configuration I could construct,
and Y does in one. It was decided by a ladder frozen before I saw either patch, and each
cell's outcome was decided by the API's own 400 rather than by my judgement. But as a
statement about the two *workflows*, it carries **no information at all** — the margin
traces to an engineer's free HTTP probe that either arm could have run, and Arm A simply
did not think to run that particular one. A one-cell margin on a grid I designed is exactly
the kind of result that should not be load-bearing in a product decision, and I would not
want it cited as evidence for the tool.

---

## 3. Efficiency

| | **Arm A** (no tool) | **Arm B** (Upshift) |
|---|---:|---:|
| elapsed wall clock | **83 min** | **112 min** (+35 %) |
| setup | 9 min | 14 min (8 base + 6 Upshift orientation) |
| migration complete at | ~74 min active, of which ~35 min lost to test-infra mishaps → **~39 min of migration proper** | **40 min** |
| tool calls | 96 | 84 |
| source files inspected | 28 | 34 (20 gptme + **14 in the product's own source**) |
| files modified | 3 | 3 |
| lines changed | 506 + / 28 − | 396 + / 17 − |
| tests added | 19 | 21 |
| **own API spend** | **$0.80** | **$0.206** |
| **Upshift API spend** | — | **$1.4248** |
| **total spend** | **$0.80** | **$1.63** |
| attended minutes attributable to the tool | — | **~25** (install 2, reading the product's source 6, hand-writing the adapter 11, sim 1, launch 2, interpreting output 3) |

Both arms' `lines_changed` reconcile **exactly** with my own `git diff --numstat` against
the pinned SHA (A: 506/28; B: 396/17). Both metrics files are accurate about the artifact.

Two things this table hides and should not:

* **The migrations took the same time.** Arm A's 74 "active" minutes include ~35 minutes
  lost to test-infrastructure accidents it documents honestly (a contaminated Tier-2
  baseline, then two concurrent suites killing each other's xdist workers). Net of those,
  both engineers diagnosed and patched in about 40 minutes. The tool did not make the
  migration faster; the 35 min gap in the totals is verification strategy, not workflow.
* **Arm B spent less of its own money *because* it used free probes** ($0.206 vs $0.80),
  and then spent $1.42 on the tool on top. Rejected requests bill nothing; both arms
  discovered this independently and Arm A said so explicitly ("all 400-returning probes
  billed $0.00, which is why the strongest before/after evidence in the report was free").
  The cheapest, highest-yield technique in this entire experiment is a two-line `curl`, and
  neither arm needed a product for it.

### 3.1 Arm A's `api_calls: 0` against $0.80 — a filling error, not a credibility problem

It is a **filling error in a single integer field**, and the surrounding record is sound:

* The adjacent `api_cost_basis` enumerates the population — 17 non-interactive gptme
  sessions, 11 raw wire probes, plus M5 checks — so the arm plainly did not claim zero
  calls.
* Its itemised breakdown sums to **$0.794**, consistent with the reported $0.80 to the
  cent: `0.002 + 0.04 + 0.13 + 0.17 + 0.20 + 0.012 + 0.19 + 0.05`.
* It states its method per line item (gptme's own end-of-session cost line for sessions;
  the returned `usage` object at published rates for probes) and its accuracy (±$0.01).
* **Independent corroboration from my own instrument:** Arm A reports *"M4 baseline
  gpt-5.6-sol N=5 — $0.13"*. I measured the same thing, blind, on the same model at N=5,
  through the same application, and recorded **$0.1301**. An exact match on a figure
  neither of us could see the other compute.

I therefore treat Arm A's $0.80 as sound and `api_calls: 0` as an unfilled default. It
should be corrected to a real count (the basis implies roughly 70–90 requests), but it does
not undermine the figures. Every other quantitative claim in that file I was able to check
independently — line counts, the $0.13 baseline, the 61 pre-existing failures' character —
held.

For symmetry I applied the same scrutiny to Arm B's file and to the two product claims it
makes. **Both verified against the product's source:**
`src/upshift/pricing.py` carries `"gpt-5.6-sol": (4.00, 20.00)` where the published rate
and gptme's own registry both say $5/$30 — so the baseline leg is under-priced by ~$0.09
(6.8 % of the run), and `--max-cost-usd` is enforced against the understated number. And
`src/upshift/capture/server.py:43` is `MESSAGES_PATH = "/v1/messages"` with
`capture/adapt.py:46` `ENDPOINT = "messages"` — capture really is Anthropic-only, so the
zero-source-reading onboarding path does not exist for an OpenAI application. Arm B
recorded the **higher** of the two cost figures throughout and did not reconcile in the
tool's favour.

---

## 4. Is there ANY evidence of a safer / more correct / more maintainable migration traceable to the tool?

I looked for it specifically. **No.**

* **Traceable to the tool: nothing in the patch.** Arm B's `_repairs_summary` reads *"0 of
  3 accepted. Upshift proposed no repair that was adopted. Every line of the shipped patch
  was written by me."* I verified the negative from the other side: I read both patches
  before unblinding and found no construct in either that resembles Upshift's repair
  playbook — no prompt-patch text, no effort-ladder edit, no endpoint-routing patch of the
  shape the tool emits.
* **The one genuinely new finding (U4) is inert.** A tool-preference shift from `save` to
  `shell`+`printf` is real and is the kind of thing only an A/B at N=5 can see. It appears
  in neither patch, in neither arm's test suite, and in neither `RESULT.md` as a change.
  It did not make the migration safer because nothing was done with it.
* **Regression protection came from the application, not the tool.** Upshift caught zero
  collateral regressions. What actually protected both arms was gptme's own suite — and
  concretely, Arm B notes that its `_resolve_reasoning_effort` signature change *"would
  have broken several of its tests had I not defaulted the new argument"*. That is the
  frozen suite doing the job, in both arms.
* **The tool's scope was structurally below the question.** Upshift's own verdict block
  stamped the run `adapted_agent` and printed *"this proves the behaviour of the adapted
  reconstruction of the agent, not of the application's own code path."* That is admirable
  honesty and it is also a concession that the run could not answer the question the
  protocol asked. The evidence that did answer it — live N=5 through gptme's own
  `llm_openai.chat` — Arm B produced by hand, and I reproduced independently.
* **Counterfactual check.** Is there a story where building the adapter forced a closer
  reading that produced the winning finding? The log does not support it: adapter
  construction required reading the prompt and tool-schema code; the endpoint-dependence
  came from a standalone probe script aimed at `max`, `verbosity`, `top_logprobs` and
  strict tools. Arm A read the same files and found the same three blocking defects with no
  adapter at all. The only thing Arm A missed was one probe it did not think to run.

**In the other direction**, there is measurable evidence the tool made this migration
*worse*: 25 attended minutes and $1.42 for zero artifact change; two false positives
generated by the fiction it required the engineer to write; a repair candidate proposing
*"never claim nothing is available when search returned flights"* to a terminal coding agent
with no flights and no search tool; a 6.8 % under-report on the very ceiling that is meant
to contain spend; and a `STAY PINNED` verdict contradicting the ground truth I measured
independently.

**The strongest thing I can say for it**, and I want it on the record because it is real:
its statistical discipline is better than most tools of its kind. It rejected a repair
candidate that restored the broken case during screening because the restoration did not
hold over the combined 2N reps; it printed its own detection floor at N=5 and warned that
four simultaneous comparisons were not multiple-comparison corrected; the free simulator
validated the whole adapter for $0 in about a second; and the budget ceiling held. A tool
that refuses to overclaim about its own evidence is rare. None of that helped here, because
the binding constraint on this migration was never statistical rigour — it was knowing
which of five documented changes apply, and that came from reading the source and from
seven free HTTP 400s.

---

## 5. Verdict

### **LOSS** — on this trial, for this application, on this evidence.

Not "modest win", not "tie". A tie would mean the tool cost something and returned
something of equal value. Here the arm that used it was, by its own contemporaneous log and
by my independent measurement, **worse off**: it paid ~60 % of its migration time and 7× its
own API budget for one inert observation and a verdict that was wrong in the direction that
blocks safe work. Subtract Upshift from Arm B and you get the same patch, 25 minutes
earlier, $1.42 cheaper, without a false `STAY PINNED` to argue past.

### What this result does and does not establish

**Does:** on an OpenAI application whose breaking changes surface as HTTP 400s and which
carries its own test suite, Upshift v0.5.0 adds cost and setup and does not improve the
outcome. That is now the **second** controlled comparison with that shape — the 2026-09-11
lovia study returned a TIE in which Upshift produced no run, no verdict and no flag. This
one is worse than that tie: it produced a run, and the run's headline was wrong.

**Does not:** refute the product's actual thesis. Upshift exists for breakage that is
*behavioural* — no 400, no failing test, just a worse agent. This application gave it the
least favourable possible conditions: every blocking change announced itself as a free 400,
and 11 054 existing tests stood guard against collateral damage. Under those conditions any
behavioural-diff tool is redundant by construction. One application is not a market
conclusion, in either direction — I said so before unblinding and I say it now.

**Three things that are not conditions of this trial and would follow the product
anywhere**, all verified by me in the product's own source or in my own measurements:
1. `capture` is hardcoded to Anthropic `/v1/messages`. For an OpenAI application the
   advertised zero-source-reading path **does not exist**, and its absence is what forced
   the 26 minutes of hand-written fiction that produced 2 of the run's 4 signals as false
   positives. This is not adverse conditions; it is missing coverage of the largest API
   surface in the market.
2. The verdict's scope is `adapted_agent`, and the tool says so itself. A verdict about a
   reconstruction the user invented is a verdict about the user's invention. The
   `native_application` scope that would fix this exists in the product and was priced out
   of reach here (~$15–40) — which means on a frontier-priced model the honest scope is the
   unaffordable one.
3. A false `STAY PINNED` is the product's worst failure mode, because the product's whole
   value proposition is the confidence to move. It occurred here, and it occurred because
   a user-authored check asserted a tool by name.

### The cheapest experiment that would change my mind

Run the same protocol on an application where the breaking change is **behavioural and
silent** — no 400, no failing test — with `capture` supporting the application's actual
provider so the adapter is recorded rather than invented, and with the `native_application`
scope affordable. If Upshift finds a regression there that a careful engineer with the
docs and the app's tests does not, that is the result the product needs and neither of the
two studies so far has tested for. If it does not find one there either, the thesis is
falsified on its own home ground.

I would not shut down or continue on the strength of my tie-break. I would decide on the
two things this study *did* establish cleanly: the tool contributed nothing to either
artifact, and its one verdict was wrong.
