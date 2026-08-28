# upshift diff

`gpt-5.5 @ chat_completions`  ->  `gpt-5.6-sol @ chat_completions`

- provider: openai-flex
- n_reps: 5
- baseline run: `pilot-55-base`
- candidate run: `pilot-56-cand`

## Summary

- baseline: 8/8 cases pass (100.0%, CI 67.6-100.0%)
- candidate: 0/8 cases pass (0.0%, CI 0.0-32.4%)

8 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| edge_impatient_duplicate_phrasing | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_no_availability | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_book_cheapest_from_search | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_city_names_to_iata | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_second_cheapest | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_by_flight_id | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_existing_booking | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_and_book_cheapest | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/pilot-56-cand/cases/<case>/rep_k.json
