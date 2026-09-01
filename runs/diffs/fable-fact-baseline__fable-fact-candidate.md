# upshift diff

`claude-fable-5 @ messages`  ->  `claude-fable-5-1 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `fable-fact-baseline`
- candidate run: `fable-fact-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Summary

- baseline: 0/2 cases pass (0.0%, CI 0.0-65.8%)
- candidate: 0/2 cases pass (0.0%, CI 0.0-65.8%)

2 stable-fail

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| q1_2025_second_highest_revenue | stable-fail | 0/5 | 0/5 | p=1 | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| tech_sector_companies | stable-fail | 0/5 | 0/5 | p=1 | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/fable-fact-candidate/cases/<case>/rep_k.json
