# upshift diff

`sim-fable-5 @ messages`  ->  `sim-fable-5-1 @ messages`

- provider: sim
- n_reps: 5
- baseline run: `a085-sim-baseline`
- candidate run: `a085-sim-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Verdict: STAY PINNED

reason: 5 of 5 regressed cases still fail after the repair budget

still regressed: box_10x20x30, box_with_hole, cube_10mm, cube_20mm, cube_40mm

## Summary

- baseline: 5/5 cases pass (100.0%, CI 56.6-100.0%)
- candidate: 0/5 cases pass (0.0%, CI 0.0-43.4%)

5 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| box_10x20x30 | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| box_with_hole | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| cube_10mm | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| cube_20mm | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| cube_40mm | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/a085-sim-candidate/cases/<case>/rep_k.json
