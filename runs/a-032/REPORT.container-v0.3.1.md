# upshift diff

`claude-fable-5 @ messages`  ->  `claude-fable-5-1 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `a-032-baseline`
- candidate run: `a-032-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Verdict: SAFE WITH PATCH

restored 6/6 regressed, 0 previously-passing broken

patch: /case/runs/a-032/upgrade.patch

repair log:

- repair start: 6 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [model_params] remove-forced-tool-choice — Drop the forced tool_choice param (rejected by this model) and state the same requirement as an instruction in the system prompt instead.
-   screen: 6/6 broken cases restored — running full verification
-   ACCEPTED: restored ['fsm_scenario_000', 'fsm_scenario_001', 'fsm_scenario_002', 'fsm_scenario_008', 'fsm_scenario_032', 'fsm_scenario_038']; 0 previously-passing cases broken; 0 regressed case(s) remain
- repair end: all 6 regressed cases restored

## Summary

- baseline: 6/7 cases pass (85.7%, CI 48.7-97.4%)
- candidate: 0/7 cases pass (0.0%, CI 0.0-35.4%)

6 regressed · 1 stable-fail

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| fsm_scenario_000 | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| fsm_scenario_001 | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| fsm_scenario_002 | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| fsm_scenario_008 | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| fsm_scenario_038 | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| fsm_scenario_032 | regressed | 4/5 | 0/5 | p=0.0238 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| fsm_scenario_012 | stable-fail | 0/5 | 0/5 | p=1 | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/a-032-candidate/cases/<case>/rep_k.json
