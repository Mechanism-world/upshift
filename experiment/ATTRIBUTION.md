# Attribution — what Upshift uniquely contributed

Built from Arm B's `CHRONO_LOG.md`, written as the work happened specifically so that
"the tool told me something I did not know" can be separated from "the tool confirmed
something already written down". Verified by the coordinator against Upshift's own run
records in `w2/upshift-runs/`.

## Classification of every Arm B finding

| id | finding | class | verified |
|---|---|---|---|
| U1 | tools on Astra require `/v1/responses` | **A** | logged 19:07, before Upshift ran; the adapter already declared `endpoint: responses` |
| U2 | Astra rejects effort `none`/`minimal` — **the one blocking defect** | **A** | logged 19:07; confirmed by the arm's own free 400-probes 19:18. Upshift never tested effort at all |
| U3 | accepted effort set is endpoint-dependent (`max` OK on responses, 400 on chat) | **A** | arm's own probe; not in the fact sheet, not from Upshift |
| U4 | **Astra prefers `shell`+`printf` over the dedicated `save` tool** (4/5 vs baseline 4/5 the other way) | **B** | `upshift-runs/gptme-astra/diff.json` — the single genuine Upshift-first finding |
| U5 | Astra spends more assistant turns self-verifying (7 vs 6) | **C** | already documented in ASTRA_FACTS.md; Upshift measured it happening |
| U6 | `temperature`/`top_p` not sent | **F** | the arm's adapter carried `params: {}` because the arm wrote it that way |
| U7 | `c3_exact_write` regressed 4/5 -> 1/5 | **false positive** | task completed correctly every rep; the check asserted a tool *by name* |
| U8 | `c4_no_overreach` improved 0/5 -> 5/5 | **false positive** | adapter artifact: the fake backend didn't implement commands the baseline issued |

**Score: 1 x B, 1 x C, 3 x A, 1 x F, 2 false positives, 0 x D, 0 x E.**

- **D (Upshift proposed the successful repair): none.** Three repair candidates screened,
  all three rejected. Every line of the shipped patch was written by the engineer.
- **E (Upshift caught collateral regression normal tests missed): none.** The regression
  protection that actually worked was gptme's own test suite.

## Verdict correctness — verified by the coordinator, not taken on trust

`w2/upshift-runs/gptme-astra/verdict.json`:

```
verdict: STAY PINNED          regressed: ['c3_exact_write']       restored: 0
baseline_passing_cases: 1     improved: ['c4_no_overreach']       accepted_patches: []
flaky: ['c1_count_lines', 'c2_noninteractive_proceed']            scope: adapted_agent
```

The verdict is **wrong for this application, in the dangerous direction**: it would have
blocked a migration that is in fact safe, and both arms independently produced a working
one. It rests on a single case (`c3_exact_write`) whose check asserted a tool by name
while the task in fact completed correctly every repetition.

**A structural point the coordinator found in the record, which Arm B did not name:**
`baseline_passing_cases: 1` of 4 — three of the four cases were flaky or failing on the
BASELINE model. Upshift ships a `BASELINE_BROKEN` guard, but it fires only when the
baseline passes *zero* cases. A suite where the baseline passes 1 of 4 is barely more
informative than one where it passes none, and it produced a confident-looking verdict
anyway. The guard's threshold is the finding, not the case.

## What Upshift did well (recorded so it is not lost in the verdict)
1. **It refuses to overclaim scope.** It printed, unprompted: *"Verification scope:
   adapted_agent ... this upgrade is NOT verified in the application."* True, unflattering,
   and printed by default.
2. **Its statistics are disciplined.** A repair candidate that restored the broken case
   during screening was then rejected because the restoration did not hold over 2N reps.
   It printed the N=5 detection floor and warned that four simultaneous tests are not
   multiple-comparison corrected. That is the opposite of p-hacking.
3. **The free simulator earned its place** — validated the whole adapter for $0 in ~1s.
4. **`--max-cost-usd` behaved** — the run stayed inside its ceiling.

## Product defects hit (recorded, NOT fixed — the experiment freeze holds)
| defect | consequence |
|---|---|
| `capture` is Anthropic-only (`/v1/messages` hardcoded) | the zero-source onboarding path does not exist for an OpenAI app; the adapter had to be *invented* rather than recorded — 26 min, and the source of both false positives |
| repair playbook is not agent-generic | offered a terminal coding agent prompt text about *flights*; spent real money screening it |
| cost table stale for `gpt-5.6-sol` ($4/$20 vs published $5/$30) | understates that leg by 6.8%, and `--max-cost-usd` enforces against the understated number |
| `--flex` advertised as cheapest | returned 429 on the baseline model while succeeding on the candidate — asymmetric availability across the two arms of the very comparison the tool exists to make |
| `BASELINE_BROKEN` threshold is zero-passing | a 1-of-4-passing baseline still yields a confident verdict (coordinator finding) |

## The shape of the limitation
Upshift **measures the agent you describe to it.** On this application the two most
interesting defects — the reasoning-effort table and the strict-schema 400 — both lived in
request-building code that the adapter format does not model. The request it never builds
is the request that breaks.
