# Upshift: what it actually contributed to this migration

Arm B. Upshift **v0.5.0**, commit `002dd600a53b0789654d2ca4c9a59b47d8437c61`, installed
from `/Users/atilavahedian/Desktop/exp914/upshift-src`. Upshift was **not modified**.

The minute-by-minute record this analysis is built on is `CHRONO_LOG.md`, which was
written **as the work happened**, specifically so that "the tool told me something I did
not know" can be told apart from "the tool confirmed something I had already written down".

---

## 1. Bottom line

**No. Not on this migration, on this evidence.**

Upshift cost ~25 minutes of integration work — about 60% of the 40 minutes the whole
migration took — and $1.42, and returned **one** finding I did not already have: a
tool-selection shift (§U4). It did not find the blocking defect, did
not propose any accepted repair, and caught no collateral regression. Its headline output —
`STAY PINNED` — was **wrong for this application**, and wrong in the dangerous direction:
it was produced by a brittle assertion in an eval case I had written myself twenty minutes
earlier, and a less careful engineer would have blocked a safe migration on it.

The honest one-line evidence statement: *on an OpenAI application whose breaking changes
are all visible in the source and in the API's own 400 bodies, Upshift added one real
behavioural observation, at a cost of ~25 minutes and $1.42, and produced a verdict that
contradicted the correct answer.*

Two things it did well are recorded in §6; they are real and they are not enough.

---

## 2. Time and money

> **Correction.** My first draft of this table claimed 57 minutes. That was built from
> running estimates that had drifted about 2x against the real clock (start 18:53:13Z; the
> whole diagnose-and-patch phase was over by 19:33Z). The figures below are re-derived
> against real timestamps. The conclusion does not change; the numbers got smaller, and I
> would rather correct them downward than leave an inflated charge against the tool.

| | minutes (wall clock) | cost |
|---|---|---|
| Install (`uv tool install .`, verify `--version`) | 2 | $0 |
| Reading README / `checks.py` / `schemas.py` / `sim.py` / `pricing.py` / `capture/` to find a usable path | 6 | $0 |
| **Hand-writing the adapter** (agent.json, system_prompt.txt, tools.json, backend.py, 4 cases) | 11 | $0 |
| Free sim validation run | 1 | $0 |
| Launching the live run (then ~12 min unattended in the background) | 2 | $1.425 |
| **Interpreting the output** (reading 4 case transcripts to find out that 3 of its 4 signals were artifacts) | 3 | $0 |
| **Total attributable to Upshift** | **~25 min** | **$1.425** |

The denominator that matters is not the 3-hour allowance — it is the time the migration
itself took. Everything up to and including the finished patch was done by **19:33Z, 40
minutes after starting**; the rest of the session was verification (the full Tier-2 suite)
that would have run with or without Upshift. So roughly **25 of those 40 minutes, about
60%, went to Upshift** — and none of it is work I would have done otherwise. I did not
need a reconstructed agent to migrate gptme; the backend, the tool schemas and the cases
exist only to feed the tool.

`upshift cost` reports **$1.3344**; my independent recomputation is **$1.425**. See §5.

---

## 3. Every Upshift command run

| # | command | outcome | min | $ |
|---|---|---|---|---|
| 1 | `uv tool install .` | OK, 0.5.0 | 2 | 0 |
| 2 | `upshift --help`, `upgrade --help` | orientation | 1 | 0 |
| 3 | `upshift upgrade --agent upshift-agent --provider sim --baseline-model sim-5.5 --candidate-model sim-5.6-sol --tag gptme-sim --runs-root upshift-runs --quiet` | `SAFE`, adapter mechanically valid | 1 | 0 |
| 4 | `upshift upgrade --agent upshift-agent --provider openai --baseline-model gpt-5.6-sol --candidate-model gpt-6-astra --tag gptme-astra --runs-root upshift-runs --n 5 --max-cost-usd 2.20 --budget 6` | `STAY PINNED` (exit 1) | 2 attended, ~12 unattended | 1.425 |
| 5 | `upshift cost --runs-root upshift-runs` | per-leg ledger | 1 | 0 |

`upshift adapt` was **not** run: it makes paid model calls over the whole repo and gptme is
a ~4,400-star, several-hundred-file codebase; that was not a defensible use of a $3.50 cap.
`upshift capture` **could not** be run — see §4.1. `upshift verify-patch` was not run: it
verifies that an exported patch reproduces the configuration a run verified, and Upshift
proposed no accepted repair, so there was no Upshift patch to verify. My patch is verified
by gptme's own test suite and by a live N=5 run through gptme's own code path.

---

## 4. Findings, classified

Key: **A** = I would likely have found it from docs/code first (and per the log, did).
**B** = Upshift surfaced it *before* I knew it. **C** = Upshift measured/verified something
I already knew. **D** = Upshift proposed the successful repair. **E** = Upshift caught a
collateral regression the normal workflow missed. **F** = no meaningful value.

| id | finding | class | true positive | evidence |
|---|---|---|---|---|
| U1 | tools on Astra require `/v1/responses` | **A** | yes | logged as K3 at 19:07, before Upshift ran on anything; my adapter already declared `endpoint: responses` because I knew |
| U2 | Astra rejects effort `none`/`minimal` — **the one blocking defect** | **A** | yes | logged as K2 at 19:07; confirmed by my own free 400-probes at 19:18. Upshift never tested effort at all: my agent sent none |
| U3 | the accepted effort set is **endpoint-dependent** (`max` OK on responses, 400 on chat completions) | **A** | yes | my probe2 script, run before the live Upshift leg finished. Not in the supplied fact sheet, not from Upshift |
| U4 | **Astra prefers `shell` + `printf` over the dedicated `save` tool** (4/5 vs sol's 4/5 the other way) | **B** | yes (as a behaviour; the "regression" label is not) | `upshift-runs/gptme-astra/diff.json`; `runs/gptme-astra-candidate/cases/c3_exact_write/rep_01..03.json` |
| U5 | Astra spends more assistant turns on self-verification (7 vs 6) | **C** | yes | `ASTRA_FACTS.md` already documents "verifies more thoroughly than necessary"; Upshift measured it happening. Strictly C, not B |
| U6 | `temperature`/`top_p` are not sent (M2 already satisfied) | **F** | n/a | I got this wrong first (K1), and I corrected it myself by re-reading `llm_openai.py:1124` after my own control probe P-G returned an unexpected 400 on the CURRENT model. Upshift's agent carried `params: {}` because I wrote it that way — it could not have found this |
| U7 | `c3_exact_write` **regressed** 4/5 → 1/5 | — | **false positive** | task completed correctly every time; my check asserted a tool *by name*. Upshift's verdict rests on this |
| U8 | `c4_no_overreach` **improved** 0/5 → 5/5 | — | **false positive** | adapter artifact: gpt-5.6-sol issued shell commands my fake backend didn't implement and honestly said so |

**D: none.** Three repair candidates were screened; all three were rejected. Every line of
the shipped patch was written by me.

**E: none.** Upshift caught no collateral regression. The regression protection that
actually worked on this migration was gptme's own test suite (Tier 1 640 passed, and the
`_resolve_reasoning_effort` signature change would have broken several of its tests had I
not defaulted the new argument).

**Score on its own terms: 1 × B, 1 × C, 3 × A, 1 × F, 2 false positives, 0 D, 0 E.**

### 4.1 Product bugs and limitations hit

| what | what it blocked | worked around how |
|---|---|---|
| **`capture` is Anthropic-only.** `/v1/messages` is hardcoded (`capture/server.py:43`, `capture/adapt.py:46`, `ENDPOINT = "messages"`). The README presents capture as *the* answer for framework agents and every row of its framework table sets `ANTHROPIC_BASE_URL`. | The zero-source-reading onboarding path, for an **OpenAI** application. This is the path that would have made the adapter honest — recorded bytes instead of my reconstruction. | Hand-wrote all five adapter files. 26 minutes. This is the single largest integration cost. |
| **The repair playbook is not agent-generic.** It offered a terminal coding agent: *"never claim nothing is available when search returned flights"*. This agent has no flights and no search tool. | Nothing — but it spent real money screening a candidate that could not possibly have applied, and it is the kind of output that destroys trust in the rest of the report. | Ignored. |
| **Cost accounting under-reports.** `pricing.py:56` has `gpt-5.6-sol` at $4/$20; the published rate and gptme's own registry say $5/$30. | Baseline leg reported $0.2568, actually ~$0.3472 — **7% of the run understated**. `--max-cost-usd` is enforced against the understated number, so the ceiling is looser than advertised on any stale model. | Recorded both figures; used the higher one throughout. Not reconciled in the tool's favour. |
| `--flex` is offered as "usually the cheapest option", but flex returned **429** on `gpt-5.6-sol` while succeeding on `gpt-6-astra` — asymmetric availability across the two arms of the very comparison the tool exists to make. | Not blocking; it made the documented 50% saving unusable here. | Ran standard tier on both. Recorded. |

### 4.2 Manual work required around the tool

| what | min | why |
|---|---|---|
| Hand-write `backend.py` (a fake shell + filesystem, 150 lines) | 12 | no capture path for OpenAI; the loop needs executable tool semantics |
| Hand-write 4 eval cases with checks and sim oracle plans | 10 | there is no way to reuse gptme's own `gptme/eval/` suite through the adapter path |
| Distil gptme's system prompt and strict tool schemas by reading `prompts/templates.py` and `llm_openai.py:2253` | 4 | the adapter must be *my* reconstruction of the agent, and its fidelity is my problem |
| Read four transcripts to work out that 3 of 4 reported signals were artifacts | 6 | the verdict alone was actively misleading |

---

## 5. Cost figures side by side

| leg | Upshift `cost` | my recomputation | note |
|---|---|---|---|
| baseline (gpt-5.6-sol) | $0.2568 | **$0.3472** | Upshift uses $4/$20; published rate $5/$30 |
| candidate + 3 screens + 1 verify (gpt-6-astra) | $1.0776 | $1.0776 | agrees exactly at $10/$50 |
| **total** | **$1.3344** | **$1.4248** | Upshift understates by $0.090 (6.8%) |

---

## 6. What Upshift did well — the case for the defence

These are real and I do not want them lost in the verdict.

1. **It refuses to overclaim scope.** The verdict block says, unprompted: *"Verification
   scope: adapted_agent … this proves the behaviour of the adapted reconstruction of the
   agent, not of the application's own code path. This upgrade is NOT verified in the
   application."* That is a true and unflattering statement about its own evidence, printed
   by default. Most tools do not do this.
2. **Its statistics are disciplined.** One repair candidate restored the broken case during
   screening and was then **rejected** because the restoration did not hold over the
   combined 2N reps. It also prints the detection floor for N=5 and warns that four
   simultaneous tests are not multiple-comparison corrected. That is the opposite of
   p-hacking, and it is rare.
3. **The free simulator earned its place.** `--provider sim` validated my whole adapter for
   $0 in about a second and would have caught an authoring error before it cost money.
4. **`--max-cost-usd` behaved.** The run stayed inside the ceiling.

None of this changes the answer. A tool that is honest and rigorous about a measurement
whose *inputs* I had to invent is still measuring my invention. On this application the
binding constraint was never statistical rigour — it was knowing which of five documented
changes apply, and that came from the source and from seven free HTTP 400s.

---

## 7. Where Upshift would have earned its place

To be fair about the conditions rather than the tool:

- If gptme's breaking change had been **behavioural rather than a 400**, the A/B at N=5 is
  the only instrument here that could have found it. U4 is a small demonstration of exactly
  that, and it is the one thing in this report I could not have got any other way.
- If `capture` supported OpenAI, the adapter would have been recorded rather than invented,
  which removes the single largest cost *and* the source of both false positives — and U7
  and U8 would probably not have happened.
- If the **native runner** could have driven `gptme/eval/`, the scope would have been
  `native_application` and the result would have been about gptme. That path exists in the
  product; it was unaffordable here (~$15–40 at N=5 on a $10/$50 model with gptme's full
  tool-documentation prompt), not unavailable.

The product's own framing — "the only scope that means verified in the application" — is
the right one, and it is the scope this migration could not afford to reach.


---

## 8. One more thing Upshift never saw

After the Upshift run was finished and its verdict written, I went looking for M5
(strict structured output) through gptme's own `--output-schema` path and found that it
returns HTTP 400 on **both** models, 5/5 each — a pre-existing, user-facing defect in the
application (`RESULT.md` P7). Upshift could not have found it: `output_schema` is not a
concept its adapter format has, so the request it never builds is the request that breaks.

That is not a criticism of the tool so much as a statement of its shape. It measures the
agent you describe to it. Everything you fail to describe is invisible, and on this
application the two most interesting defects — the reasoning-effort table and the strict
schema — both lived in request-building code that the adapter format does not model.
