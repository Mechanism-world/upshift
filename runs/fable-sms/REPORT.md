# upshift diff

`claude-fable-5 @ messages`  ->  `claude-fable-5-1 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `fable-sms-baseline`
- candidate run: `fable-sms-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Verdict: SAFE WITH PATCH

restored 5/5 regressed, 0 previously-passing broken

patch: runs/fable-sms/upgrade.patch

repair log:

- repair start: 5 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [model_params] remove-forced-tool-choice — Drop the forced tool_choice param (rejected by this model) and state the same requirement as an instruction in the system prompt instead.
-   screen: 4/5 broken cases restored — running full verification
-   ACCEPTED: restored ['email_on_file_requested', 'gibberish_still_calls_a_tool', 'greeting_texts_back', 'order_help_with_username']; 0 previously-passing cases broken; 1 regressed case(s) remain
- candidate 2/6: [prompt_edit] prompt-execute-dont-ask — Append an execute-don't-interrogate block: when a request already contains everything a tool needs, call it instead of asking for details the tool does not require (observed on real gpt-5.6-sol).
-   screen: 0/1 broken cases restored — rejected without full verification
- candidate 3/6: [prompt_edit] prompt-ground-in-results — Append a results-grounding block: never claim nothing is available when search returned flights (observed on real gpt-5.6-sol).
-   screen: 0/1 broken cases restored — rejected without full verification
- candidate 4/6: [model_params] reasoning-effort-high — Raise reasoning_effort to 'high' (fallback for unclassified behavioral failures).
-   screen: 0/1 broken cases restored — rejected without full verification
- candidate 5/6: [model_params] raise-effort-one-rung — Raise reasoning_effort one rung to 'xhigh' on the messages ladder (effort is only ever raised, never lowered).
-   screen: 1/1 broken cases restored — running full verification
-   ACCEPTED: restored ['order_help_asks_for_username']; 0 previously-passing cases broken; 0 regressed case(s) remain
- repair end: all 5 regressed cases restored

## Summary

- baseline: 5/5 cases pass (100.0%, CI 56.6-100.0%)
- candidate: 0/5 cases pass (0.0%, CI 0.0-43.4%)

5 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| email_on_file_requested | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| gibberish_still_calls_a_tool | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| greeting_texts_back | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| order_help_asks_for_username | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| order_help_with_username | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/fable-sms-candidate/cases/<case>/rep_k.json
