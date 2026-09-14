# FINAL REPORT — does Upshift beat a competent engineer with Claude Code, docs and tests?

Experiment run 2026-09-14. Target: **gptme/gptme @ `3411afec`**, `gpt-5.6-sol` -> `gpt-6-astra`.
Three isolated agents: Arm A (normal workflow), Arm B (normal workflow + Upshift v0.5.0
@ `002dd60`), Arm C (independent evaluator, blinded).
Protocol and pre-registration committed BEFORE any result existed (`a8c5356`, `31df54c`).

---

## Decision table

| Metric | Claude + tests (A) | Claude + tests + Upshift (B) |
|---|---:|---:|
| Migration successful | **Yes** | **Yes** |
| Evaluator acceptance | **SUCCESS** | **SUCCESS** |
| Total elapsed time | **83 min** | 112 min |
| Setup/integration time | **9 min** | 14 min (+ ~25 min Upshift integration) |
| API cost | **$0.80** | $1.62 ($0.21 own + $1.42 Upshift) |
| Tests executed | 22,797 | 51,000 |
| Confirmed migration bugs found | 3 blocking + 3 recorded | 2 blocking + 4 recorded |
| Important bugs missed | 0 | 0 |
| False positives | 0 | **2** (both adapter artifacts) |
| Manual source changes | 3 files, +506/−28 | 3 files, +396/−17 |
| Final regressions | **0** | **0** |
| Human/founder assistance | 0 | 0 |

Independent evaluator, blinded: **both patches SUCCESS**; Tier 1 (651) and Tier 2 (11,054)
at **zero regressions** against a 3-run pinned-SHA baseline; M1–M4 pass, M5 NOT EXERCISED;
mechanical test-weakening checks W1–W7 clean for both — **neither arm modified a single
pre-existing test, conftest, pytest config, Makefile or CI file.**

---

## The central finding

**The two arms produced substantially the same migration, independently — roughly 80%
identical.** The evaluator, still blinded, found that both added the same optional `tools`
parameter to `_should_use_responses_api` with the same override semantics; both created a
module-level table *literally named* `_MODELS_REQUIRING_RESPONSES_FOR_TOOLS` containing
exactly `"gpt-6-astra"`; both quoted the same 400 body and flagged the same `'none'` trap;
and both independently found the least obvious thing in either patch — that upstream's
three `startswith("gpt-5")` branches silently stop matching at GPT-6.

The blinded tie-break went to **Candidate X, which unblinded to Arm B — the Upshift arm** —
by **one cell out of eighteen**: X models the accepted reasoning-effort set *per endpoint*,
Y does not.

**That margin is not Upshift's.** Arm B's own contemporaneous log classifies that finding
as **class A**, produced by its own probe script, with the evidence note: *"Not in the
supplied fact sheet, not from Upshift."* Upshift never tested reasoning effort at all in
that run. The Upshift arm won the comparison on work the engineer did without the tool.

---

## The ten questions

**1. Did both approaches successfully migrate the application?** Yes. Both SUCCESS on
blinded evaluation, zero regressions across ~11,700 tests, M1–M4 live at N=5.

**2. Did Upshift discover a meaningful problem Arm A missed?** **One, and it did not change
the patch.** Upshift's A/B at N=5 measured that Astra prefers `shell` + `printf` over
gptme's dedicated `save` tool (4/5 reps, versus the baseline preferring `save` 4/5) — a
genuine behavioural shift, class **B**, the single finding traceable to the tool. Arm A did
not find it. It is real, and it is the one thing in this experiment obtainable no other way.
It also did not alter the shipped migration.

**3. Did Upshift prevent a bad migration Arm A would have shipped?** **No — it attempted the
opposite.** Its verdict was `STAY PINNED`, which is wrong for this application, in the
dangerous direction: it would have blocked a migration that both arms independently proved
safe. Verified by the coordinator in `verdict.json`, not taken on trust. The verdict rests
on a single case whose check asserted a tool *by name* while the task completed correctly
every repetition. A less careful engineer would have blocked a safe upgrade on it.

**4. Did Upshift materially reduce time or engineering work?** **No — it increased both.**
+29 minutes elapsed (83 -> 112) and roughly double the cost. Arm B's own accounting: ~25 of
the 40 minutes its migration took, about 60%, went to Upshift, and none of it was work it
would otherwise have done.

**5. Did Upshift materially increase confidence with evidence unavailable from normal
tests?** **Partially, and it is the strongest thing in its favour.** The N=5 A/B is the only
instrument here that could detect a behavioural shift that raises no API error, and it did
detect one. But the confidence it produced was scoped to a *reconstruction* of the agent,
not the application — which the tool itself says in its own output — and its headline
conclusion from that evidence was wrong.

**6. How much extra setup did Upshift require?** ~25 minutes, of which 26 minutes of raw
work was hand-writing five adapter files (a 150-line fake shell/filesystem backend, four
eval cases, a distilled system prompt and tool schemas), because **`capture` is
Anthropic-only** — `/v1/messages` is hardcoded — so the zero-source onboarding path the
README presents as *the* answer does not exist for an OpenAI application.

**7. Was that setup worth it?** **No.** It bought one behavioural observation that did not
change the patch, two false positives, and an incorrect verdict.

**8. What did Claude/Codex already solve so easily that Upshift was redundant?**
Essentially the whole migration. The binding constraint was never statistical rigour — it
was knowing which of five documented changes actually apply to this codebase, and that came
from reading the source and from a handful of free HTTP 400s. Rejected requests bill $0, so
the strongest before/after evidence in both arms was free.

**9. Single strongest Upshift contribution?** The tool-selection shift (U4) — a real
behavioural regression risk, invisible to every test in gptme's 12,000-test suite, found by
N=5 statistical comparison. Runner-up, and genuinely unusual: it **refuses to overclaim
scope**, printing unprompted *"this upgrade is NOT verified in the application"*, and its
statistics are disciplined — it rejected a repair candidate that screened green but failed
over 2N reps, and printed its own N=5 detection floor.

**10. Single strongest argument against Upshift?** **It measures the agent you describe to
it.** The two most interesting defects in this migration — the reasoning-effort table and
the strict-schema 400 — both live in request-building code the adapter format does not
model. The request it never builds is the request that breaks. And because the adapter must
be invented rather than recorded, the tool's inputs are the engineer's assumptions, which is
where both false positives and the wrong verdict came from.

---

## Product defects found (recorded, NOT fixed — the experiment freeze held)

| defect | consequence |
|---|---|
| `capture` is Anthropic-only (`/v1/messages` hardcoded) | no zero-source path for OpenAI apps; adapter invented not recorded — largest single cost, and source of both false positives |
| repair playbook not agent-generic | offered a terminal coding agent prompt text about *flights*; spent real money screening it |
| pricing stale for `gpt-5.6-sol` ($4/$20 vs published $5/$30) | understates that leg 6.8%; `--max-cost-usd` enforces against the understated number |
| `--flex` advertised as cheapest | 429 on the baseline model while succeeding on the candidate — asymmetric across the two arms of the comparison the tool exists to make |
| `BASELINE_BROKEN` fires only at zero passing cases | this run had `baseline_passing_cases: 1` of 4 and still produced a confident verdict (coordinator finding) |

---

## Verdict: **LOSS** — reached independently by the coordinator and the blinded evaluator

Against the founder's own definition — *"Upshift adds more complexity/time than value"* —
this is not close. It added 35% elapsed time, consumed ~60% of the migration window,
produced two false positives, proposed zero accepted repairs, caught zero collateral
regressions, and returned a verdict wrong in the direction that negates its proposition.

The evaluator, unblinded and asked explicitly not to soften: *"Not a tie. A tie would mean
it cost something and returned something of equal value. Arm B was measurably worse off."*
Its cost framing is sharper than mine: **$1.42 for the tool is about 7x Arm B's entire
direct API spend of $0.206.** Subtract Upshift and you get the same patch, 25 minutes
earlier, $1.42 cheaper, with no false block to argue past.

### The tie-break must not be cited as a rescue
Arm B won rung 1 of the evaluator's grid. The evaluator disowns that as evidence about the
workflows, and proves the mechanism: **Arm B's adapter declared `params: {}`, so no Upshift
request ever carried a reasoning effort on either endpoint. A tool that never sends the
parameter cannot discover which values it accepts.** The evaluator found the same fact
independently, while blind, with one `curl` that cost **$0** — rejected requests bill
nothing. *"As a statement about the two patches my ranking stands; as a statement about the
two workflows it carries no information. A one-cell margin on a grid I designed myself
should not be load-bearing in a shutdown decision."*

### Data quality: both arms' numbers hold
Arm A's `api_calls: 0` against $0.80 is **a filling error in one integer, not a credibility
problem.** The evaluator settled it independently: Arm A reports its `gpt-5.6-sol` N=5
baseline at **$0.13**; the evaluator measured the same quantity blind at **$0.1301**. It
applied equal scrutiny to Arm B and confirmed both of its product claims directly in the
source.

---

## What this means for Mechanism

**Three findings are not artifacts of this trial and follow the product anywhere:**
1. `capture` has no OpenAI path at all — the advertised zero-source-reading onboarding does
   not exist for the largest API surface in the market.
2. The affordable verification scope is `adapted_agent` — a verdict about the user's own
   reconstruction of their agent, not about their application.
3. **A false `STAY PINNED` is the worst failure mode a confidence product can have**, because
   confidence to move is the entire proposition.

**Three studies now point the same way.** 2026-09-08 (shell_gpt): our own records concluded
Upshift "did not find the fix — the docs and the 400 say it." 2026-09-11 (lovia): TIE, no
run produced. 2026-09-14 (gptme): LOSS — and, as the evaluator notes, *worse* than the tie,
because that study produced no verdict while this one produced a wrong one.

**The strongest honest counter-argument, which I am obliged to put as forcefully as the
verdict:** this application gave Upshift the least favourable conditions possible. Every
blocking change announced itself as a free HTTP 400, and 11,054 passing tests stood guard.
Upshift exists for breakage that is *behavioural and silent*. **No study so far has tested
the thesis on its home ground** — and the one thing that did work here (N=5 detecting a real
tool-selection shift invisible to all 12,000 of gptme's tests) is exactly the mechanism the
thesis rests on.

### The recommendation

Do not continue the company on the strength of the product as it exists. The evidence
against it is now three-for-three, and the three structural findings above are not fixable
by polish — capture has no OpenAI path, and the affordable scope verifies a reconstruction
rather than an application.

But there is exactly one experiment left that could change the answer, and it is cheap and
well-specified: **same protocol, on an application whose breaking change is behavioural and
silent, with `capture` supporting its provider so the adapter is recorded rather than
invented, and `native_application` scope affordable.** That is the thesis on its home ground.

The honest framing of the choice: the *detection* wedge is dead — three studies say the
breaks are loud, documented, and cheap to find, and that competent engineers close these
migrations in ~40 minutes for under a dollar without weakening a test. What remains
unfalsified is the *silent behavioural drift* wedge, which is a different product and a
narrower market: it needs a customer whose agent degrades in ways their tests do not catch,
who will pay to know. **That is a customer-discovery question, not an engineering one, and
it should be answered before another line of product is written.**

If you want to spend one more week, spend it finding that customer — not building. If you
cannot find them, the Friday decision is made.

---

## Artifacts
- [PROTOCOL.md](PROTOCOL.md) · [PRE_REGISTRATION.md](PRE_REGISTRATION.md) · [ERRATA.md](ERRATA.md) · [ASTRA_FACTS.md](ASTRA_FACTS.md) · [ATTRIBUTION.md](ATTRIBUTION.md)
- Arm A: `~/Desktop/exp914/w1/{RESULT.md,final.patch,metrics.json}`
- Arm B: `~/Desktop/exp914/w2/{RESULT.md,final.patch,metrics.json,UPSHIFT_CONTRIBUTION.md,CHRONO_LOG.md}`
- Evaluator: `~/Desktop/exp914/eval/{EVALUATION_PLAN.md,SCORING.md,COMPARISON.md,DEPARTURES.md,results/LEDGER.md}`
- Spend: **$3.4053 of $8.00** authorised; every sub-cap respected, reserve untouched.
