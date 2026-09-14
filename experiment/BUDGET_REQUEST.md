# Budget status and request — 2026-09-14

## Remaining authorized budget: $0 for THIS experiment

Checked `~/Desktop/upshift-rescue-ops/sprint/budget.json`, which is the only spend
ledger of record.

- $10.00 authorized 2026-09-08 by the founder in chat ("yes, $10 cap authorized"),
  scope: **Astra validation**.
- Re-scoped 2026-09-11 ("approved, use the remaining $7.40 for the comparison"),
  scope: **the lovia A/B comparison**.
- Spent to date: **$4.2857**. Unspent balance: **$5.71**.
- The ledger closes that authorization explicitly: *"Nothing further will be spent."*

Both authorizations name a specific study. Neither names this one. Per the standing
instruction not to assume old campaign budgets apply, I am treating the $5.71 remainder
as **closed, not available**, and have made **no paid call**.

Environment is otherwise ready: the OpenAI key authenticates (HTTP 200 on the free
`/v1/models` endpoint), and `gpt-6-astra` is present and accessible. Whether the account
currently holds credit is NOT verified — verifying it requires a billable call. It
billed successfully on 2026-09-11, three days ago.

## Smallest reasonable amount: $8.00

Sized from our own measured records, not guessed. Basis: the 2026-09-08 Astra run
(14 cases, N=5, full pipeline) cost $2.4752 measured, i.e. ~$0.0093 per repetition on
Astra and ~$0.0076 on gpt-5.5.

| Line | Estimate | Sub-cap |
|---|---:|---:|
| Arm A — baseline, development probes, final validation | ~$1.10 | $1.75 |
| Arm B — same engineering probes + Upshift legs (baseline, candidate, repair screens, final) | ~$2.90 | $3.50 |
| Evaluator — pinned-SHA baseline + fresh live Astra validation of BOTH candidates | ~$1.20 | $2.00 |
| Reserve (reasoning-token variance, retries) | ~$2.50 | $0.75 |
| **Total** | **~$5.50** | **$8.00** |

Each arm gets a hard `--max-cost-usd` sub-cap. Spend is logged per leg and reconciled
against the ledger.

## Why I do not recommend shrinking this below ~$8

The cheap way to cut cost is to lower N (repetitions per case) or drop cases. Both
directly sabotage the thing under test: Upshift's entire claim is that N-repetition
statistical comparison catches behaviour single-shot tests miss. Running the experiment
at N=1-2 would guarantee a finding of "no added value" for reasons that have nothing to
do with whether the product works. That would be a rigged experiment, and a rigged
experiment is worse than no experiment when the decision is whether to keep the company.

If $8 is not available, the honest move is to not run the paid phase at all and decide
on the offline evidence plus the two prior studies — not to run a cheap version.

## What has been done at $0 so far
Repository selection, full documentation verification of Astra (recorded in
ASTRA_FACTS.md), frozen protocol, pre-registration, evaluator plan, and all offline
migration work both arms can do without a live call.
