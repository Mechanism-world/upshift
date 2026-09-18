# upshift diff

`stub-model-a @ chat_completions`  ->  `stub-model-b @ chat_completions`

- provider: sim
- n_reps: 3
- baseline run: `verify-patch-native-sim-baseline`
- candidate run: `verify-patch-native-sim-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Verdict: STAY PINNED

reason: 3 of 3 regressed cases still fail after the repair budget

still regressed: add_one_task, answers_without_extra_tools, append_to_existing_list

at N=3 reps per case, NO per-case degradation reaches p<0.05 on a one-sided Fisher exact test — this run could not have detected even a total collapse. A non-significant difference is not evidence of equivalence.

collateral protection was not exercised on this run: no case passed on the candidate before repair.

Verification scope: native_application

the application's own entry point ran, with its own request-building code and the original incident configuration.

evidence ids: verify-patch-native-sim-baseline=8e88a5d947c4  verify-patch-native-sim-candidate=b5e96f1ee501

## Summary

- baseline: 3/3 cases pass (100.0%, CI 43.9-100.0%)
- candidate: 0/3 cases pass (0.0%, CI 0.0-56.1%)

3 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| add_one_task | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| answers_without_extra_tools | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| append_to_existing_list | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

3 test(s) performed, one per case; p is NOT adjusted for multiple comparisons, so at alpha=0.05 roughly 1 of 3 could reach significance by chance alone. Read a single starred p as a pointer to a transcript, never as a result on its own.

full transcripts: runs/verify-patch-native-sim-candidate/cases/<case>/rep_k.json
