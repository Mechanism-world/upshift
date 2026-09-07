# Product reliability upgrade — working file (sprint started 2026-09-07)

Format per finding: `finding → source → current status → reproduction → planned fix →
acceptance test → final evidence`. Statuses: Already fixed and verified · Still reproducible
· Partially fixed · Capability gap · Access or credential blocker · Not an Upshift defect.

## 0. Starting state (established before any edit)

- Working tree: `/Users/atilavahedian/Desktop/Upshift` on `fix/adapt-implicit-concat-and-56-pricing`
  (5cac358, 3 ahead of main), one untracked file `result.md` (ops summary, not product
  code). No stash. Stale worktrees pruned. Remote `origin` = github.com/Mechanism-world/upshift.
- `origin/main` = 7bd03e5, `pyproject` version `0.4.1.dev0`. Last release `v0.3.1`; main is
  105 files / +14,302 lines past it (capture mode v0.4.0-dev, three capture fixes incl.
  `BASELINE_BROKEN`, version stamp, tool-field reporting, Anthropic-track fixes). None of that
  is released.
- Baseline suite on the fix branch: **1317 passed**, ruff clean.
- Branch triage (all against origin/main):

| branch | content | status | action |
|---|---|---|---|
| fix/upgrade-max-cost | superset of the OpenAI-track stack: adapt implicit-concat join, gpt-5.6-terra/luna + 5.2/5.2-pro/5-mini pricing, `/v1/responses` forced-tool_choice flattening, output-token-cap translation, `--max-cost-usd` ceiling (`budget.py`) | pending, conflicts with main in agent_loop/cli/CHANGELOG/tests | merge (union) into `integrate/v0.5` |
| fix/responses-token-cap-param, fix/responses-forced-tool-choice, fix/adapt-implicit-concat-and-56-pricing | strict subsets of the above | superseded by fix/upgrade-max-cost | not merged separately |
| fix/token-cap-param-repair | gpt-4o-mini/4.1-nano pricing; gpt-5 token-cap-parameter 400 repair; loop signature-priority fix | pending, conflicts in playbook/test_pricing | merge (union) |
| fix/gpt-4o-mini-pricing | subset of the above | superseded | — |
| fix/gpt-54-pricing | pricing rows | pending | merge |
| fix/anthropic-runs-marked-simulated | same fix as merged fix/anthropic-runs-stamped-simulated | superseded (duplicate) | drop |
| claude/funny-gates-ff1bf7 | `volatile_suffix` on the adapter | superseded: main implements `volatile_suffix` (schemas, agent_loop, ADAPTER.md) | drop |
| dependabot PRs #1 #2 | actions bumps | open | merge after CI |

- Rescue artifacts: private ops repo cloned at `~/Desktop/upshift-rescue-ops` (accessible);
  `result.md` summarises the OpenAI track (109 terminal cases: 9 REPAIRED_VERIFIED, 2
  REPAIR_FAILED, 2 NO_REGRESSION, 4 BASELINE_BROKEN, 54 UNSUPPORTED_FRAMEWORK, 33
  SOURCE_INVALID, 3 COST_BLOCKED, 2 PRIVATE). Anthropic track: 52 cases (36 UNSUPPORTED_FRAMEWORK
  → capture mode). Findings digest: see §1 (filled from the ops artifacts).

## 1. Findings

(filled as the digest and reproductions land)

| # | finding | source | current status | reproduction | planned fix | acceptance test | final evidence |
|---|---|---|---|---|---|---|---|
| 1 | Repair verified via reconstructed request ≠ verified in the upstream application | rescue REPAIRED_VERIFIED cases (adapter-only verification) | Capability gap | — | scope labels (DESIGN §A), `verify-patch` (§E), native runner (§C) | rescue suite #9, #12 | |
| 2 | Endpoint conversion misses config semantics (reasoning, output-cap precedence, state params, seed, extra_body, silent drops, swallowed errors) | ops cases + fix branches (token-cap, tool_choice) | Partially fixed (token cap + tool_choice on branches; rest unverified) | — | table-driven map_params + matrix tests (§G) | rescue suite #2 #3 #4 #5 | |
| 3 | "Zero collateral" with no protected passing cases | capture A-075 → BASELINE_BROKEN guard on main; other cases | Partially fixed (all-fail baseline blocked; protection-not-exercised not reported) | — | collateral block + wording (§D) | rescue suite #1 #8 | |
| 4 | Real projects needed hand-written Python backends | 4 Claude agents (edit ledgers), OpenAI cases | Capability gap | — | native runner (§C) + capture replay scope | native runner tests, representative case before/after | |
| 5 | Capture loses fields / cannot represent beyond recorded turns | capture fixes on main (per-turn params, tool fields); continuation | Partially fixed | — | continuation policy (§F), field coverage tests | rescue suite #6 #7 | |
| 6 | Bedrock / Vertex / Message Batches gaps | ops cases | Capability gap / credential blocker | — | provider boundary; implement offline-testable parts only if a recorded case justifies | labelled tests | |
| 7 | Product changes spread across branches/worktrees | git state above | Still reproducible → integrated in `integrate/v0.5` | — | this sprint's integration | one branch, one release | |
| 8 | UNSUPPORTED cases that were framework bugs, not migration regressions | ops UNSUPPORTED_FRAMEWORK list | Not an Upshift defect (classification) | — | `NOT_A_MIGRATION` classification in reports where evidence exists | — | |
| 9 | Negative/zero budget reached paid execution | v0.3.1 audit | Already fixed and verified (test_cli_init) | | | | |
| 10 | Security: git option injection, symlink escape, generated-code injection, tag/run-id traversal, container limits | v0.3.1 audit | Already fixed (tests/test_security.py) — re-verify on integrated tree | | | rescue suite #12 | |
