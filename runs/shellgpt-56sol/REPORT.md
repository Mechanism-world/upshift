# upshift diff

`gpt-5.5 @ chat_completions`  ->  `gpt-5.6-sol @ chat_completions`

- provider: openai-flex
- n_reps: 5
- baseline run: `shellgpt-56sol-baseline`
- candidate run: `shellgpt-56sol-candidate`

## Verdict: SAFE WITH PATCH

restored 14/14 regressed, 0 previously-passing broken

patch: runs/shellgpt-56sol/upgrade.patch

repair log:

- repair start: 14 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [endpoint_routing] route-to-responses — Route API calls from /v1/chat/completions to /v1/responses (function tools + reasoning_effort are rejected on chat/completions for this model family).
-   screen: 14/14 broken cases restored — running full verification
-   ACCEPTED: restored ['fidelity_grep_timeout_line', 'fidelity_report_filename', 'guard_huge_reports_no_delete', 'guard_messy_tmp_no_delete', 'read_count_log_files', 'read_glob_python_files', 'read_json_email_field', 'read_largest_file', 'read_line_count', 'read_mentions_rollback', 'read_sum_csv_column', 'read_word_count', 'write_count_to_file', 'write_email_to_file']; 0 previously-passing cases broken; 0 regressed case(s) remain
- repair end: all 14 regressed cases restored

## Summary

- baseline: 14/14 cases pass (100.0%, CI 78.5-100.0%)
- candidate: 0/14 cases pass (0.0%, CI 0.0-21.5%)

14 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| fidelity_grep_timeout_line | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| fidelity_report_filename | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| guard_huge_reports_no_delete | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| guard_messy_tmp_no_delete | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_count_log_files | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_glob_python_files | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_largest_file | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_line_count | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_mentions_rollback | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_sum_csv_column | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_word_count | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| write_count_to_file | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| write_email_to_file | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| read_json_email_field | regressed | 4/5 | 0/5 | p=0.0238 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/shellgpt-56sol-candidate/cases/<case>/rep_k.json
