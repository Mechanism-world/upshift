# Reliability upgrade — v0.5.0 (engineering report, 2026-09-07)

Companion to `PRODUCT_RELIABILITY_UPGRADE.md` (the working file with finding → source →
status → fix → test → evidence). This report explains what was wrong, what changed, and
what is still not verified. Every claim points at a test or a committed artifact.

## 1. What was actually wrong

The migration-rescue campaign (109 OpenAI-track and 54 Anthropic-track cases in the private
ops repo) succeeded in places by hand: research agents rebuilt applications as five-file
adapters, translated adapter patches into upstream diffs by hand, and worked around product
gaps on unmerged branches. Read against the records, the product had these defects:

1. **Verification scope was invisible.** Every "REPAIRED_VERIFIED" rested on a reconstructed
   agent directory; nothing in a verdict, report or terminal line said so. The prisma
   maintainer showed a "verified" repair did not fix the incident; the accepted ozwellai
   repair required code the target did not have.
2. **Endpoint conversion silently changed semantics.** `params` were forwarded verbatim except
   for three translations. A `store: true` in params overrode the managed `store: false`;
   `previous_response_id` / `conversation` rode through into one-shot replays; `seed` was
   forwarded to an endpoint without one; a chat-shaped forced `tool_choice` produced
   `400 Missing required parameter: 'tool_choice.name'` on the only repair the gpt-5.6
   break calls for; the output cap raised a `TypeError` inside the SDK that killed whole paid
   runs; `response_format` was deleted by `adapt` with a note claiming upshift owned it.
3. **"Zero collateral" was hard-coded.** `verdict.py` wrote `broken_by_patch: 0` "by
   construction"; in 15 of 15 lab repairs the protected set was empty, so the guard never
   had a chance to fire, and the campaign summary published "6 of 6 zero-collateral".
4. **Greedy acceptance foreclosed better repairs.** The loop accepted the first candidate
   that restored anything; a sibling that restored everything was never screened (waku:
   wrong STAY PINNED; crispen: the maintainer's own fix hidden; ozwellai: the unshippable
   repair accepted). Adjudication could be skipped by a cost ceiling and the verdict still
   said SAFE WITH PATCH.
5. **Non-behavioural failures counted as behaviour.** Local SDK `TypeError`s were labelled
   with a manufactured HTTP 400; flex-capacity 429s became "permanent" failing reps (58% of a
   baseline in one case); an exhausted capture recording silently recycled the last turn.
6. **Hand-written backends everywhere.** `adapt` never produced a usable `backend.py` on an
   OpenAI-track case; TypeScript targets got nothing; 16 of 25 lab targets had their own test
   harness and exactly one was used. "The hand-write again cost more than every run
   combined."
7. **Fixes lived on eight unmerged branches**, some stacked, some duplicates; five case
   results depended on them; one lab binary matched no branch.
8. Docs said "nothing leaves your machine" while provider calls carry prompts and keys and
   `adapt` sends code slices to the extraction model.

## 2. What was fixed (with the test that pins it)

| area | change | tests |
|---|---|---|
| Scope | `scope` derived (`request_contract` / `adapted_agent` / `native_application`) into manifests, rep records, verdict.json, REPORT.md and terminal output; "NOT verified in the application" printed for every non-native run | `tests/test_native_runner.py`, `tests/test_verdict_integrity.py`, `tests/test_cli_e2e_v05.py` |
| Patch verification | `upshift verify-patch`: clean copy + `git apply` of the exact exported patch, every case's first request rebuilt through `build_request` and compared with the verifying run; `make_patch` now raises on divergence a diff cannot carry and emits the no-newline marker | `tests/test_verify_patch.py`, `tests/rescue/test_rescue_09_*.py` (38/38 byte-identical on the sim run) |
| Native runner | `agent.json` `runner` block; protocol v1 over stdin/stdout; `--allow-runner` gate; minimal env; workdir copy per rep; timeout/process-group kill; output cap; `--capture` on loopback for wire evidence; Python and Node reference runners; runner failures are `runner_error` (non-behavioural) | `tests/test_native_runner.py` (44), e2e in `tests/test_cli_e2e_v05.py` |
| Translation | table-driven `translate_params` (reasoning ladders per provider, output-cap spellings with native precedence, tool_choice shapes, sampling placement, seed → `determinism: best_effort`, state-linking dropped and recorded, `response_format` ↔ `text.format`); managed fields cannot be overridden; every drop/passthrough recorded; `TranslationError` never substitutes (never turns reasoning off) | `tests/test_translation_matrix.py` (positive/negative/precedence per row, meta-tests enforce coverage), `tests/rescue/test_rescue_02..05` |
| Error classes | `sdk_validation` (no manufactured status), `harness_error` signature (never repaired), `transient_provider_error` with bounded per-rep retries and `--retry-errored`; billing/auth never retried | `tests/test_translation_matrix.py`, `tests/test_billing_guard.py`, retry tests in `tests/test_runner_retry*.py` |
| Verdicts | `INCONCLUSIVE` with reason codes (empty suite, incomplete run, billing/auth, runner/continuation/transient errors, cost ceiling, missing run, model unavailable, mixed sim/live evidence); `BASELINE_BROKEN` kept; collateral measured (`protected_cases`, `checks_executed`, `exercised`) with the explicit "not exercised" sentence; fresh final verification run distinct from selection runs; evidence identity with a refusing resume guard; SAFE wording says what was measured; `detectable_effect` stated; Fisher/Wilson checked against independent references | `tests/test_verdict_integrity.py`, `test_collateral.py`, `test_final_verification.py`, `test_evidence_identity.py`, `test_stats_reference.py`, `tests/rescue/test_rescue_01/08/10/11` |
| Repair loop | every sibling candidate screened, ranked by full restoration → no disclosure → rank; adjudication cannot be skipped (cost ceiling ⇒ INCONCLUSIVE); flaky-band contributions never count; signature priority derived from the differ; capability/cost-changing repairs disclosed and ranked last | `tests/test_sibling_screening.py`, `test_adjudication_required.py`, `test_token_cap_repair.py` |
| Schema repair | `schema-strict-compat` for the structured-output 400s (OpenAI strict rules, disclosed) | `tests/test_schema_repair*.py` |
| Capture | continuation policy (`fail` default, `repeat_last` explicit); machine-readable `unsupported_fields.json`; `response_format` carried; nullable unions canonicalised | `tests/test_capture_continuation.py`, `test_capture_fidelity.py`, `test_jsonschema*.py` |
| Security | all v0.3.1 protections re-verified on the integrated tree; new: credentials never in records/logs, minimal container env, capture upstream operator-supplied only, injection provenance | `tests/test_security.py`, `tests/rescue/test_rescue_12_security_fixture.py` |
| Docs | real data-flow wording (README, SECURITY.md), `docs/capabilities.md` with three evidence levels, `docs/provider-matrix.md` with unverified rows marked, ADAPTER.md native runner + continuation + schema rows | — |
| Integration | eight branches triaged: two stacks merged (union), two superseded branches dropped, Dependabot bumps applied | this branch's merge commits |

## 3. What it can do now that it could not before

- Say, in every output, whether a result is a request-contract reproduction, an adapted
  agent, or the application itself — and refuse to imply the last without the runner.
- Run an application's own eval/test command per case and rep with the same statistics and
  verdict engine, in Python or Node, without a Python backend rewrite.
- Prove that the exported patch is the verified patch (request-level equality), and refuse
  to reuse a run whose inputs changed.
- Return INCONCLUSIVE instead of SAFE when the evidence cannot support a conclusion, and
  report collateral protection as measured rather than assumed.
- Translate endpoint configuration by a tested table, recording every drop, never
  disabling reasoning silently, never manufacturing an HTTP error.
- Pick the best repair among siblings, disclose capability-changing repairs, and never
  accept on skipped adjudication or on a flaky-band contribution.
- Treat transient provider errors as what they are and retry them on resume.

## 4. What still does not work or is not verified

- **No live API validation ran in this sprint** (zero spend; no budget was authorized). The
  native runner, `verify-patch`, INCONCLUSIVE paths, translation table and repair changes are
  offline-tested (simulator, mocked transport, recorded fixtures). The live matrix and its
  estimate (~$2.50) are in `PRODUCT_RELIABILITY_UPGRADE.md` §2.
- **`verify-patch --live`** is documented as future work; the command verifies request
  equality, not a fresh paid run.
- **Emitting the upstream source diff** (as opposed to the adapter diff) is not automated.
  The native runner verifies a patch the developer supplies; the capture mapping table
  names per-framework knobs; translating an adapter patch into application code remains
  human work and is stated as such.
- **Bedrock / Vertex / Message Batches**: not implemented (14 rescue cases). No recorded case
  can be reproduced offline; nothing was substituted with the direct API.
- **Request headers** as an adapter surface (`parallel_tool_calls: false` via a header in two
  cases) are not representable.
- **Provider matrix**: rows marked "unverified" (e.g. whether `seed` exists on `/v1/responses`)
  forward and record rather than encode a rule.
- **Type checking**: the project has no configured type checker; an informational mypy pass
  reports 40 findings on `src/upshift`. Not gated.
- Old committed run records (pre-v0.5) carry no evidence ids and no collateral block; reports
  say so instead of inventing them. Committed verdicts are unchanged.

## 5. Test results by category (integrated tree, all offline)

| category | tests |
|---|---|
| translation + detectors | 275 |
| providers, sim, agents, CLI, pricing | 386 |
| verdicts, evidence, statistics, repair loop | 202 |
| adapt | 189 |
| capture | 169 |
| rescue regression suite (maintenance coverage) | 62 |
| security | 55 |
| native runner + CLI end-to-end (simulator) | 50 |
| other | 609 |
| **total** | **1997 passed, 0 skipped** |

Lint: ruff clean on `src`, `tests`, `agents`. CI: lint, tests (ubuntu + macOS), package build
+ twine + clean tool-install sim demo, pip-audit (blocking).

## 6. Manual setup before / after (representative cases, from the ops records)

| case | before | after (v0.5.0) |
|---|---|---|
| auditk (A-052, own `BenchmarkToolHarness`) | five adapter files hand-built, `backend.py` re-implemented the app's tool semantics, a bespoke byte-equality script; scope `adapted_agent` at best | `runner` block (~12 lines) + cases + a ~40-line shim printing protocol v1; scope `native_application`; no prompt/tools/backend rewrite |
| litellm capture (A-075) | zero-edit adapter from `adapt --from-capture` but a false SAFE over an all-failing baseline | same zero edits; BASELINE_BROKEN + per-turn `tool_choice` + continuation policy |
| prisma (ghi56-001) | `max_completion_tokens` killed the paid run with a TypeError; incident's `reasoning_effort: high` untested | cap translated, reasoning carried to `reasoning.effort`, the incident config is a rescue fixture |
| waku (ghisdk-127) | STAY PINNED although a full repair existed one candidate away | sibling screening picks the 9/9 candidate (test) |

Effort hours were not recorded by the campaign; edit counts are in the ops ledger.

## 7. Spend

Live API cost of this sprint: **$0.00** (no live calls; the smoke/fixture tests are offline).
The campaign's own ledger is corrected in the working file (published OpenAI $20.30 double-
counted ~$0.92; record-backed $19.23; Anthropic $7.20 reconciles).
