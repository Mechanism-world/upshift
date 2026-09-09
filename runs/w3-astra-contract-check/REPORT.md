# upshift diff

`gpt-5.5 @ chat_completions`  ->  `gpt-6-astra @ chat_completions`

- provider: openai
- n_reps: 1
- baseline run: `w3-astra-contract-check-baseline`
- candidate run: `w3-astra-contract-check-candidate`

## Verdict: STAY PINNED

reason: 14 of 14 regressed cases fail and no repair was attempted

still regressed: fidelity_grep_timeout_line, fidelity_report_filename, guard_huge_reports_no_delete, guard_messy_tmp_no_delete, read_count_log_files, read_glob_python_files, read_json_email_field, read_largest_file, read_line_count, read_mentions_rollback, read_sum_csv_column, read_word_count

at N=1 reps per case, NO per-case degradation reaches p<0.05 on a one-sided Fisher exact test — this run could not have detected even a total collapse. A non-significant difference is not evidence of equivalence.

collateral protection was not exercised on this run: no case passed on the candidate before repair.

Verification scope: adapted_agent

the adapter's backend.py executed real tool semantics; this proves the behaviour of the adapted reconstruction of the agent, not of the application's own code path.

This upgrade is NOT verified in the application: nothing here ran the application's own entry point.

evidence ids: w3-astra-contract-check-baseline=763b427d164d  w3-astra-contract-check-candidate=e30ed7769d5e

## Summary

- baseline: 14/14 cases pass (100.0%, CI 78.5-100.0%)
- candidate: 0/14 cases pass (0.0%, CI 0.0-21.5%)

14 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| fidelity_grep_timeout_line | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| fidelity_report_filename | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| guard_huge_reports_no_delete | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| guard_messy_tmp_no_delete | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning, serialized_tool_calls | API call failed: Function tools with reasoning_effort are... |
| read_count_log_files | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_glob_python_files | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_json_email_field | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_largest_file | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_line_count | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_mentions_rollback | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_sum_csv_column | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_word_count | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| write_count_to_file | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| write_email_to_file | regressed | 1/1 | 0/1 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

14 test(s) performed, one per case; p is NOT adjusted for multiple comparisons, so at alpha=0.05 roughly 1 of 14 could reach significance by chance alone. Read a single starred p as a pointer to a transcript, never as a result on its own.

full transcripts: runs/w3-astra-contract-check-candidate/cases/<case>/rep_k.json
