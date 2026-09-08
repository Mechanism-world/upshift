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

## 1. Findings (evidence from the private ops repo, digest 2026-09-07)

| # | finding | source (ops artifact) | current status | reproduction | planned fix | acceptance test | final evidence |
|---|---|---|---|---|---|---|---|
| 1 | Adapter-verified ≠ native application: all 15 REPAIRED cases verified a reconstructed agent dir; every `human-edit.md` exists to translate the adapter patch to upstream; prisma's maintainer showed the "verified" repair did not fix the incident (`prisma-41-review/REVIEW_RESPONSE.md:9`); ozwellai's accepted repair required code the target lacks (`ghi56-004/CASE.md` Lim.5) | ops cases ghi56-001/002/004/006, ghc-223, A-075, ghisdk-127 | Capability gap | verify-patch on any committed run: none exists; scope not recorded anywhere | DESIGN §A scope labels, §E verify-patch (exact exported patch → request-building path), §C native runner (app runs itself; developer-supplied patch verified) | rescue #9, #12; verdict/report show scope | scope in every output (tests/test_cli_e2e_v05.py); verify-patch 38/38 byte-identical on sim; native runner e2e on sim |
| 2 | Endpoint conversion misses config semantics: reasoning key overwrite (last-wins), `max_completion_tokens` TypeError killed paid runs (`ghi56-001/CASE.md:214`), native cap clobbered by default (prisma review :95), `store:true` in params overrides managed `store:false`, `previous_response_id`/`conversation` pass through, `seed` forwarded to responses, nested tool_choice → 400 (dac), `extra_body`/`response_format`/`reasoning` unrepresentable in adapt, reasoning items dropped on read, service_tier/prompt_cache_key injected on both endpoints | ops STATUS_2026-09-07 :59 :81; branches b44e091/7e14c4c | Partially fixed (tool_choice flatten + cap translation merged on integrate/v0.5); rest still reproducible | unit: map_params with each param | DESIGN §G table-driven translation + records of drops; sdk_validation error class | rescue #2 #3 #4 #5 + translation matrix tests | tests/test_translation_matrix.py (275 translation+detector tests); managed fields unoverridable; drops recorded |
| 3 | "Zero collateral" was vacuous in 15/15 lab cases (candidate 0/N → empty protected set); `verdict.py` hardcodes `broken_by_patch: 0`; OpenAI EXEC_SUMMARY publishes "6 of 6 zero-collateral" unqualified; greedy acceptance hid sibling candidates (waku wrong STAY PINNED `ghisdk-127/CASE.md:196`, crispen hid the maintainer's fix `ghc-062:243`, ozwellai accepted the unshippable one) | ops CASE.md files listed in digest §3 | Partially fixed (BASELINE_BROKEN on main); collateral not reported; greedy acceptance still reproducible | scripted-provider repair() on an all-regressed suite | DESIGN §D collateral block + sentence; fresh final verification; FOLLOW-UP: screen all sibling candidates for a signature before accepting, prefer full restoration and non-capability-changing repairs | rescue #8 + collateral tests | collateral measured (test_collateral.py); sibling screening (test_sibling_screening.py); adjudication required (test_adjudication_required.py) |
| 4 | Hand-written backends in 16/16 OpenAI agent dirs; adapt not run in 15/25 lab cases (TypeScript targets); 16/25 targets had a native harness, exactly one (waku `evals/dataset.jsonl`) was used; "the hand-write cost more than every run combined" (ghi56-004:228, ghisdk-032:259) | ops ATTRIBUTION.md files | Capability gap | — | DESIGN §C native runner (Python + Node reference runners) | native runner tests; before/after on waku-style case | tests/test_native_runner.py (44) + CLI e2e; before/after table in reports/reliability-upgrade-v0.5.md §6 |
| 5 | Capture fidelity: per-turn tool_choice flattening + tie-break flip (A-075 §6.3 — fixed on main), tool schema fields dropped (fixed on main), dynamic suffix (fixed), streaming symptom unreachable (ghi56-004:203), continuation beyond recorded turns silently capped, `str \| None` union loses null (ghisdk-052:274), no tool-schema repair candidate (p2-001:236: routing 0/13 → routing+schema edit 12/13), transient 429 recorded as permanent failing rep (3 sightings) | ops A-075, ghi56-004, ghisdk-052, p2-001, ghi56-019 | Partially fixed | — | DESIGN §F continuation policy; unsupported_fields list; FOLLOW-UPS: tool-schema repair candidate; `--retry-errored`; union-null fidelity | rescue #6 #7 | continuation policy + unsupported_fields.json (test_capture_continuation/fidelity); response_format carried; nullable unions canonicalised |
| 6 | Bedrock/Vertex/Message Batches: 14 cases blocked at $0; no substitution ever performed (labeled where identified: A-001) | ops A-001…A-082, A-058/A-059, ghi56-044 | Capability gap / credential blocker | — | Not implemented this sprint: no recorded case can be reproduced offline; document as unsupported in docs/capabilities.md; provider boundary stays extensible | — | docs/capabilities.md marks Bedrock/Vertex/Batches not implemented |
| 7 | Five case results depended on unmerged branches; a lab binary matched no branch (`ghi56-006/CASE.md:241`); worktrees deleted | git state | Still reproducible → fixed by `integrate/v0.5` (7e1a728, 69ecc6c, d1545f5) | — | one integration branch, one release | release built from main | integrate/v0.5 → main, v0.5.0 |
| 8 | UNSUPPORTED closures that were framework bugs: A-098, A-013, A-115, trk-007, ghi56-018/035/016/013, p2-009 (+24-case Anthropic cohort, FRAMEWORK_REASSESSMENT.md:81) | ops state.json/CASE.md | Not an Upshift defect (triage vocabulary lacked NOT_A_MIGRATION) | — | INCONCLUSIVE(harness/sdk_validation) makes the non-migration nature visible in-product; docs name the boundary | rescue #10 | harness_error / sdk_validation / transient → INCONCLUSIVE (tests) |
| 9 | Negative/zero budget reached paid execution | v0.3.1 audit | Already fixed and verified (`test_cli_init`) | | | | |
| 10 | Security: git option injection, symlink escape, generated-code injection, tag/run-id traversal, container limits | v0.3.1 audit | Already fixed (`tests/test_security.py`) — re-verified on integrate/v0.5 (suite green) | | extend to new paths (runner, capture) | rescue #12 | tests/rescue/test_rescue_12_security_fixture.py (24) + tests/test_security.py |
| 11 | Lab spend ledger: OpenAI published $20.30 double-counts ~$0.92 (ghc-062, p2-001 pre-resume); record-backed $19.23; queue.csv is not a ledger; Anthropic $7.2034 reconciles exactly | ops LAB_SUMMARY.md vs CASE.md sums | Not an Upshift defect (hand-made summary) | — | `upshift cost` over records is the only ledger; note in report | — | |
| 12 | `--max-cost-usd` existed only on `adapt`; per-case cap breached ($7.03) inside one `upgrade`; unpriced model priced at fail-closed max tripped a ceiling at $0.013 real | ops ghisdk-052:207, ghc-223 | Fixed on integrate/v0.5 (budget.py ceiling on run/upgrade; pricing rows) | — | — | test_cost_ceiling | merged; adjudication under a ceiling ⇒ INCONCLUSIVE(cost_ceiling) |

## 2. Live validation matrix (smallest useful; NOT run — no budget authorized for this sprint yet)

| check | provider | estimate |
|---|---|---|
| verify-patch --live on shell_gpt's committed patch (fresh N=5 final run, scope adapted_agent) | OpenAI flex | ~$0.30 |
| native runner end-to-end on one Python case with its own harness (waku-style `evals/dataset.jsonl`, MIT) baseline+candidate | OpenAI flex | ~$1.50 |
| honest no-regression SAFE on a real compatible pair (example agent, 5 cases) | OpenAI flex | ~$0.50 |
| Anthropic messages translation smoke (sampling in extra_body, output cap, forced tool_choice removal) | Anthropic | ~$0.20 |

Total ≈ $2.50. Everything else in this sprint is offline (sim, mocked transport, recorded fixtures).

## 3. Follow-ups scheduled after the four streams land (owners' files)

- repair/loop.py: screen every sibling candidate for a signature before accepting; prefer full restoration; prefer candidates without disclosures; never accept a candidate whose contribution sits in the flaky band without adjudication (omnideck hunk 2).
- repair/playbook.py: a tool-parameter JSON-Schema repair candidate (p2-001; ghisdk-052 union-null).
- runner.py: transient provider errors (429/5xx) are retried per rep with a bounded policy and never recorded as permanent behavioural failures; `--retry-errored` on resume.
- adapt/generate.py: `response_format` is not "owned by upshift" — carry it as a param.
- agent_loop/providers: reasoning items on the responses read side are recorded, not ignored; service_tier/prompt_cache_key injected only on the endpoints that accept them.
- schemas/agent.json: no request-header surface — `parallel_tool_calls: false` delivered via a header (ghi56-036/022) cannot be reproduced; decide whether headers become part of the adapter contract.
- repair/loop.py: derive the loop's signature priority from `differ.SIGNATURE_PRIORITY` / `SIGNATURES_WITHOUT_REPAIRS` instead of a private copy (the token-cap regression's root cause).
- verdict.py: flex capacity 429s ("Flex does not have sufficient resources") and other transient provider errors are non-behavioural → INCONCLUSIVE, never a failure.
- playbook.py: a generic drop-unsupported-param repair (e.g. `prompt_cache_retention is not supported on this model`) and a structured-output JSON-Schema repair are open design decisions, not implemented.

## 4. Sprint outcome

All findings above have a final-evidence entry or an explicit capability-gap statement. Live validation: not run ($0 spent); matrix in §2 awaits a budget decision. Engineering report: reports/reliability-upgrade-v0.5.md.
