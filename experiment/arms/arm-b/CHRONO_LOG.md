# Chronological log — Arm B (Upshift)

> **TIMESTAMP CORRECTION, written at 19:34Z.** The inline `HH:MM` labels below were my own
> running estimates and they DRIFTED BADLY — by the end they were overstating elapsed time
> by roughly 2x. Only three timestamps in this file were actually read from the clock:
> **start 18:53:13Z**, the probe round at **19:07:26Z**, and **19:33:14Z**. The whole span
> from "19:40" to "21:36" below in fact occurred between roughly 19:08Z and 19:33Z.
> The ORDERING is accurate and is what the attribution argument rests on; the absolute
> labels are not. Corrected totals are in `metrics.json`. I am leaving the wrong labels
> visible rather than silently rewriting them.

Times are UTC. This log is written as work happens, specifically so that
"Upshift surfaced it first" (B) can be distinguished from "I already knew it" (A/C).

- 18:53:13 START. Read PROTOCOL_ARMS.md, ASTRA_FACTS.md, COMMON_RULES.md, both metric schemas.
- ~18:56 `uv tool install` Upshift 0.5.0 -> OK (exit 0). Upshift binary available. NOT yet run on anything.
- ~18:57 gptme venv created; `uv pip install -e .[server,datascience,browser]` + pytest stack. OK.
- ~19:00-19:08 SOURCE READING ONLY (no Upshift invocation of any kind yet).
  Findings recorded BEFORE Upshift has been run even once:

  K1. gptme/llm/llm_openai.py `_get_temperature()` and `_get_top_p()` special-case
      reasoning models with the substring test `"gpt-5" in model_meta.model`.
      `gpt-6-astra` does not contain "gpt-5", so it falls through to the generic path:
      temperature = constants.TEMPERATURE (0.0) and top_p = constants.TOP_P (0.1)
      are BOTH attached to the request. ASTRA_FACTS: temperature/top_p are removed
      parameters on gpt-6-astra -> HTTP 400. => predicted blocking failure. (M2)
      how_found: source reading.

  K2. `_OPENAI_REASONING_EFFORTS` (llm_openai.py ~line 1279) = {none, minimal, low,
      medium, high, xhigh, max} for ALL openai models. Astra rejects `none` with 400.
      `minimal` is also not in Astra's documented valid list (low/medium/high/xhigh/max).
      => app accepts an effort the target model refuses. (M3) how_found: source reading.

  K3. `_should_use_responses_api()` returns True for provider==openai when
      `model_meta.supports_responses_api`; the registry entry for `gpt-6-astra`
      already sets supports_responses_api=True. So under DEFAULT config the endpoint
      is already correct (M1 satisfied by default). Escape hatches that break it:
      `GPTME_OPENAI_RESPONSES_API=0/false/no/off` and `_is_proxy(client)`.
      how_found: source reading.

  K4. grep for `prompt_cache` across gptme/ -> zero hits. The documented Astra change
      `prompt_cache_retention` -> `prompt_cache_options.ttl` DOES NOT APPLY to this app.
      how_found: source reading. (Recorded so a later tool claim about it can be judged.)

  K5. `gpt-6-astra` is already in the model registry (llm_openai_models.py) for the
      `openai` provider AND is already the `openai-subscription` default. The migration
      is therefore a default-switch + correctness work, not a registry-add.

- 19:08 Coordinator authorizes $3.50 combined live budget. Baseline on gpt-5.6-sol first.

- 19:18 FREE LIVE PROBES (7 requests, all HTTP 400, zero tokens billed, $0.00).
  Script: scratchpad/probe.py. Results verbatim:
    P-A cc+tools astra              -> 400 "Function tools with reasoning_effort are not
                                      supported for gpt-6-astra in /v1/chat/completions..."
    P-B cc+tools+reasoning_effort=none -> 400 "'reasoning_effort' does not support 'none'
                                      with this model. Supported: low, medium, high, xhigh."  (THE TRAP, confirmed)
    P-C responses+temperature astra -> 400 "'temperature' is not supported with this model"
    P-D responses+top_p astra       -> 400 "'top_p' is not supported with this model"
    P-E responses+effort=none astra -> 400 "Supported values: low, medium, high, xhigh, max"
    P-F responses+effort=minimal astra -> 400 (same list; `minimal` also invalid)
    P-G CONTROL responses+temperature on gpt-5.6-sol -> ALSO 400.
  P-G is the informative one: the CURRENT model already refuses temperature, so if gptme
  really sent it today it would already be broken. That forced a re-read.

- 19:21 CORRECTION — K1 WAS WRONG, and I found it myself by re-reading llm_openai.py:1124.
  The sampling block is guarded by `if not is_reasoner:` (both Responses path line 1124 and
  Chat Completions path line 1168). `gpt-6-astra` has supports_reasoning=True, so
  temperature/top_p are NEVER attached. The `"gpt-5" in model_meta.model` substring test in
  _get_temperature/_get_top_p is unreachable for any reasoner. M2 is ALREADY SATISFIED.
  I record this because it is the kind of false lead a tool could later be credited with
  "finding" — it was mine, and it was wrong, and I corrected it myself, before running Upshift.
  Upshift has STILL NOT BEEN RUN AT THIS POINT.

  Standing real finding after the correction:
  K2 (confirmed live by P-E/P-F): _OPENAI_REASONING_EFFORTS accepts `none` and `minimal`
  for every openai model; gpt-6-astra rejects both with 400. GPTME_THINKING_EFFORT=none is
  a working configuration on gpt-5.6-sol today and becomes a hard 400 on astra. (M3)

- 19:40-19:50 Built the Upshift adapter BY HAND (agent.json, system_prompt.txt distilled
  from gptme/prompts/templates.py compact_base_prompt + non_interactive_prompt, tools.json
  modelled on gptme/llm/llm_openai.py:2253-2261 strict-tool generation, backend.py fake
  workspace, cases/cases.json with 4 cases). `capture` could not be used: it is hardcoded
  to Anthropic /v1/messages (src/upshift/capture/server.py:43, capture/adapt.py:46).
  `adapt <repo>` not used: paid model extraction over a ~4400-star repo, budget-unsafe.
- 19:52 `upshift upgrade --provider sim` -> SAFE, $0. Adapter mechanically valid.
- 19:56-20:05 Probe round 2 (2 x 400 free, 5 x 200 at ~$0.003 total). NEW, mine, not Upshift:
  * astra /v1/responses accepts reasoning effort `max` (200).
  * astra /v1/chat/completions REJECTS `max`: "Supported values are: 'low','medium',
    'high','xhigh'". So the valid effort set is ENDPOINT-DEPENDENT. ASTRA_FACTS states one
    combined list and does not say this. Confirmed twice (P-B and probe2).
  * astra accepts `verbosity` on chat completions and `text.verbosity` on responses (200),
    so gptme's `startswith("gpt-5")` verbosity gate silently drops a working feature.
  * astra rejects `top_logprobs` ("logprobs are not supported with reasoning models").
  * astra + strict tools on /v1/responses: 200. M5 wire-level sanity OK.
- 20:08 LIVE `upshift upgrade` gpt-5.6-sol -> gpt-6-astra, N=5, 4 cases. COMPLETED.
  Verdict STAY PINNED. Recorded spend $1.3344. Result table:
    c3_exact_write  regressed 4/5 -> 1/5   c2_noninteractive flaky 3/5 -> 0/5
    c1_count_lines  flaky 3/5 -> 5/5       c4_no_overreach   improved 0/5 -> 5/5
- 20:12 TRANSCRIPT READING (mine). Adjudication of the three signals:
  * c3 "regression" = astra used `shell` with `printf '1.2.0' > /srv/app/VERSION` instead
    of the `save` tool. Task COMPLETED correctly. My check asserted the `save` tool by name.
    => a real TOOL-PREFERENCE SHIFT, surfaced by Upshift; NOT a functional break; the
    "regressed" label is my brittle check.
  * c2 = astra used 7 assistant turns vs my n=6 cap; it did MORE verification and said so
    ("Verification was attempted, but the command runner returned simulated output").
    Matches the documented "verifies more thoroughly than necessary". Task completed on both.
  * c4 "improvement" = adapter-fidelity artifact: gpt-5.6-sol issued shell commands my fake
    backend did not implement and honestly reported it could not list the directory.
  NET: Upshift found ZERO genuine functional regressions. It DID surface one real
  behavioral shift (tool preference + more verification turns) that I did not know before.
- 20:12 PRODUCT DEFECT OBSERVED: the repair loop proposed booking-agent-specific prompt
  text to a terminal coding agent — "never claim nothing is available when search returned
  flights". This agent has no flights and no search tool. Playbook candidates are not
  agent-generic. Upshift did correctly REJECT the candidate that screened green but did not
  hold over 2N reps.

- 20:20-21:05 Wrote the migration patch MYSELF (no Upshift repair was accepted):
  _base_model_name/_is_gpt5_plus, _MODEL_REASONING_EFFORTS (per-model AND per-endpoint),
  _MODELS_REQUIRING_RESPONSES_FOR_TOOLS, endpoint threaded into _resolve_reasoning_effort,
  docs/providers.rst updated, 21 new offline tests.
- 20:50 LIVE N=5 through gptme's OWN code path (llm_openai.chat with a real ToolSpec,
  requests instrumented at the SDK boundary), identical config on both models:
    gpt-6-astra  5/5 responses endpoint, 5/5 no banned params, 5/5 tool loop terminates
    gpt-5.6-sol  5/5 / 5/5 / 5/5
  This is native_application evidence and it is MINE, not Upshift's (Upshift's run was
  explicitly stamped `adapted_agent` / "NOT verified in the application").
- 21:05 Tier 1 post-change: 640 passed + 1 skipped (619 existing, exactly matching the
  pre-change count, + 21 new); tests/test_tool_use.py 31/31. Zero failures.
- 21:12 Tier 2 (full broad net, ~10,000 tests) killed at ~5% after 11 min - would not fit
  the wall clock. 11 failures seen in that 5%, pre-existing status UNDETERMINED. Recorded
  as a verification gap in RESULT.md, not papered over.
- 21:33 final.patch verified: applies cleanly to 3411afec in a clean clone; 3 files, all
  application files, no experiment artifacts.
- 21:36 FINISH.

--- VERIFICATION PHASE (after the migration patch was already complete at 19:33Z) ---
- 19:33Z Full Tier 2 launched on the patched tree.
- 19:36Z Live M5 check through gptme's own --output-schema path: 400 on gpt-6-astra 5/5.
  Immediately re-ran it on gpt-5.6-sol: 400 5/5 as well => PRE-EXISTING (P7), not a
  migration regression. $0 (schema-validation 400s bill nothing). Upshift had no visibility
  into this at all: `output_schema` is not a concept its adapter format represents.
- 20:15Z Tier 2 (patched) finished: 12,278 passed / 92 failed / 136 skipped / 7 errors.
- 20:18Z Adjudicated the 92: extracted all 99 failing node ids, reverted llm_openai.py and
  providers.rst to the pinned SHA, removed my test file, ran exactly that set: 65 failed /
  34 passed. Restored the patch, ran the identical set again: 65 failed / 34 passed, and
  the sorted failure NAME SETS are identical in both directions. Zero introduced, zero fixed.
- 20:21Z Full Tier 2 launched at the pinned SHA for a complete before/after.
- 20:37Z Tier 2 (pinned SHA) finished: 12,235 passed / 114 failed / 136 skipped / 7 errors.
  Collected differs from the patched run by exactly 21 = my new tests. The patched tree has
  FEWER failures; the difference is flakiness in parallelism-sensitive server tests, which
  is why the identity comparison above is the load-bearing evidence, not the counts.
- 20:43Z Patch restored and re-verified against a clean clone of the pinned SHA.
  Tier 1 final: 671 passed, 1 skipped, 0 failed.
- 20:45Z FINISH. Total 1h52m; the migration itself took 40 min.
