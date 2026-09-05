# upshift diff

`claude-opus-4-8 @ messages`  ->  `claude-fable-5-1 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `a-085-baseline`
- candidate run: `a-085-candidate`

## Verdict: SAFE WITH PATCH

restored 5/5 regressed, 0 previously-passing broken

patch: /case/runs/a-085/upgrade.patch

repair log:

- repair start: 5 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [model_params] remove-forced-tool-choice — Drop the forced tool_choice param (rejected by this model) and state the same requirement as an instruction in the system prompt instead.
-   screen: 5/5 broken cases restored — running full verification
-   ACCEPTED: restored ['box_10x20x30', 'box_with_hole', 'cube_10mm', 'cube_20mm', 'cube_40mm']; 0 previously-passing cases broken; 0 regressed case(s) remain
- repair end: all 5 regressed cases restored

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

full transcripts: runs/a-085-candidate/cases/<case>/rep_k.json
