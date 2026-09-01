# upshift diff

`claude-fable-5 @ messages`  ->  `claude-fable-5-1 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `fable-qs-baseline`
- candidate run: `fable-qs-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Summary

- baseline: 4/5 cases pass (80.0%, CI 37.6-96.4%)
- candidate: 4/5 cases pass (80.0%, CI 37.6-96.4%)

4 stable-pass · 1 stable-fail

## Cases that changed

4 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| smoke_calculator_two_plus_two | stable-fail | 0/5 | 0/5 | p=1 | wrong_or_missing_tool_call | calculator called 0 times, min_times=1. |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/fable-qs-candidate/cases/<case>/rep_k.json
