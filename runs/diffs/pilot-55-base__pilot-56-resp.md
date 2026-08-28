# upshift diff

`gpt-5.5 @ chat_completions`  ->  `gpt-5.6-sol @ responses`

- provider: openai-flex
- n_reps: 5
- baseline run: `pilot-55-base`
- candidate run: `pilot-56-resp`

## Summary

- baseline: 8/8 cases pass (100.0%, CI 67.6-100.0%)
- candidate: 5/8 cases pass (62.5%, CI 30.6-86.3%)

5 stable-pass · 3 regressed

## Cases that changed

5 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| edge_impatient_duplicate_phrasing | regressed | 5/5 | 0/5 | p=0.00397 ** | wrong_or_missing_tool_call | book_flight called 0 times, min_times=1. |
| happy_book_by_flight_id | regressed | 5/5 | 0/5 | p=0.00397 ** | wrong_or_missing_tool_call | book_flight called 0 times, min_times=1. |
| exact_city_names_to_iata | regressed | 5/5 | 2/5 | p=0.0833 * | other_behavioral | final message does not contain 'B6220'. |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/pilot-56-resp/cases/<case>/rep_k.json
