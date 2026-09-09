# upshift diff

`gpt-5.5 @ chat_completions`  ->  `gpt-6-astra @ chat_completions`

- provider: openai
- n_reps: 5
- baseline run: `w3-astra-n5-baseline`
- candidate run: `w3-astra-n5-candidate`

## Verdict: SAFE WITH PATCH

restored 13/13 regressed, 0 previously-passing broken

at N=5 reps per case, the smallest degradation detectable at p<0.05 is 5/5 -> 1/5 (p=0.0238); anything milder cannot reach significance at this N. A non-significant difference is not evidence of equivalence — it is the absence of evidence of a difference.

collateral protection was not exercised on this run: no case passed on the candidate before repair.

Verification scope: adapted_agent

the adapter's backend.py executed real tool semantics; this proves the behaviour of the adapted reconstruction of the agent, not of the application's own code path.

This upgrade is NOT verified in the application: nothing here ran the application's own entry point.

evidence ids: w3-astra-n5-baseline=a16233059b92  w3-astra-n5-candidate=4facda3b70bb

the verdict rests on the FRESH final verification run `w3-astra-n5-final` (full suite, new seeds); the 2 screening/verification run(s) that SELECTED the candidates are listed as selection_runs and are not the evidence for them.

patch: runs/w3-astra-n5/upgrade.patch

repair log:

- repair start: 13 regressed case(s), 0 protected passing case(s), budget 24 candidates
- candidate 1/24: [endpoint_routing] route-to-responses — Route API calls from /v1/chat/completions to /v1/responses (function tools + reasoning_effort are rejected on chat/completions for this model family).
-   screen route-to-responses: 13/13 broken cases restored
-   ACCEPTED route-to-responses: restored ['fidelity_grep_timeout_line', 'fidelity_report_filename', 'guard_huge_reports_no_delete', 'guard_messy_tmp_no_delete', 'read_count_log_files', 'read_glob_python_files', 'read_largest_file', 'read_line_count', 'read_mentions_rollback', 'read_sum_csv_column', 'read_word_count', 'write_count_to_file', 'write_email_to_file']; 0 previously-passing cases broken; 0 regressed case(s) remain
- final verification w3-astra-n5-final: full suite, 5 reps, fresh seeds — 14/14 cases pass
- repair end: all 13 regressed cases restored

## Summary

- baseline: 13/14 cases pass (92.9%, CI 68.5-98.7%)
- candidate: 0/14 cases pass (0.0%, CI 0.0-21.5%)

13 regressed · 1 stable-fail

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| fidelity_grep_timeout_line | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| fidelity_report_filename | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| guard_huge_reports_no_delete | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_count_log_files | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_glob_python_files | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_largest_file | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_line_count | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_mentions_rollback | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_sum_csv_column | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_word_count | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| write_count_to_file | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| write_email_to_file | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| guard_messy_tmp_no_delete | regressed | 4/5 | 0/5 | p=0.0238 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_json_email_field | stable-fail | 2/5 | 0/5 | p=0.222 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

14 test(s) performed, one per case; p is NOT adjusted for multiple comparisons, so at alpha=0.05 roughly 1 of 14 could reach significance by chance alone. Read a single starred p as a pointer to a transcript, never as a result on its own.

full transcripts: runs/w3-astra-n5-candidate/cases/<case>/rep_k.json
