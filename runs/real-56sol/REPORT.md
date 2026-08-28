# upshift diff

`gpt-5.5 @ chat_completions`  ->  `gpt-5.6-sol @ chat_completions`

- provider: openai-flex
- n_reps: 5
- baseline run: `real-56sol-baseline`
- candidate run: `real-56sol-candidate`

## Verdict: STAY PINNED

reason: 4 of 36 regressed cases still fail after the repair budget

still regressed: edge_ambiguous_missing_date, edge_book_unknown_flight, edge_cancel_already_cancelled, edge_cancel_nonexistent, edge_impatient_duplicate_phrasing, edge_no_availability, edge_out_of_scope_hotel, edge_sold_out_flight, exact_book_cheapest_from_search, exact_cancel_by_reference, exact_cancel_then_rebook, exact_city_names_to_iata

repair log:

- repair start: 36 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [endpoint_routing] route-to-responses — Route API calls from /v1/chat/completions to /v1/responses (function tools + reasoning_effort are rejected on chat/completions for this model family).
-   screen: 27/36 broken cases restored — running full verification
-   ACCEPTED: restored ['edge_ambiguous_missing_date', 'edge_cancel_already_cancelled', 'edge_cancel_nonexistent', 'edge_no_availability', 'edge_out_of_scope_hotel', 'exact_book_cheapest_from_search', 'exact_cancel_by_reference', 'exact_iata_direct_then_book', 'exact_max_price_filter', 'exact_multiturn_departure_time', 'exact_nonstop_and_budget_then_book', 'exact_nonstop_flag', 'exact_price_cap_then_book', 'exact_reverse_direction', 'exact_second_cheapest', 'happy_book_earliest_departure', 'happy_book_only_nonstop', 'happy_cancel_existing_booking', 'happy_cancel_one_of_two', 'happy_cancel_then_confirm', 'happy_multiturn_book_by_time', 'happy_report_cheapest_price', 'happy_search_and_book_cheapest', 'happy_search_basic', 'happy_search_then_decline', 'happy_two_leg_searches']; 0 previously-passing cases broken; 10 regressed case(s) remain
- candidate 2/6: [prompt_edit] prompt-execute-dont-ask — Append an execute-don't-interrogate block: when a request already contains everything a tool needs, call it instead of asking for details the tool does not require (observed on real gpt-5.6-sol).
-   screen: 5/10 broken cases restored — running full verification
-   ACCEPTED: restored ['edge_sold_out_flight', 'exact_cancel_then_rebook', 'exact_passenger_name_hyphenated', 'happy_book_by_flight_id', 'happy_book_then_cancel']; 0 previously-passing cases broken; 5 regressed case(s) remain
- candidate 3/6: [prompt_edit] prompt-ground-in-results — Append a results-grounding block: never claim nothing is available when search returned flights (observed on real gpt-5.6-sol).
-   screen: 3/5 broken cases restored — running full verification
-   adjudication edge_sold_out_flight: verify 3/5 + extra 5/5 = 8/10 — cleared
-   adjudication exact_nonstop_flag: verify 3/5 + extra 2/5 = 5/10 — CONFIRMED
-   REJECTED: relapsed earlier-restored case(s) ['exact_nonstop_flag']
- candidate 4/6: [model_params] reasoning-effort-high — Raise reasoning_effort to 'high' (fallback for unclassified behavioral failures).
-   screen: 3/5 broken cases restored — running full verification
-   ACCEPTED: restored ['happy_search_multiple_options']; 0 previously-passing cases broken; 4 regressed case(s) remain
- candidate 5/6: [prompt_edit] prompt-verbatim-identifiers — Append a verbatim-identifiers block: report flight/booking ids exactly as tools returned them (observed on real gpt-5.6-sol).
-   screen: 3/4 broken cases restored — running full verification
-   adjudication exact_nonstop_flag: verify 3/5 + extra 5/5 = 8/10 — cleared
-   adjudication happy_book_then_cancel: verify 3/5 + extra 4/5 = 7/10 — CONFIRMED
-   REJECTED: relapsed earlier-restored case(s) ['happy_book_then_cancel']
- all current candidates rejected; giving up
- repair end: 32/36 regressed cases restored; unrestored: ['edge_book_unknown_flight', 'edge_impatient_duplicate_phrasing', 'exact_city_names_to_iata', 'exact_date_written_out']

## Summary

- baseline: 36/38 cases pass (94.7%, CI 82.7-98.5%)
- candidate: 0/38 cases pass (0.0%, CI 0.0-9.2%)

36 regressed · 1 flaky · 1 stable-fail

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| edge_ambiguous_missing_date | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_book_unknown_flight | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_cancel_already_cancelled | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_cancel_nonexistent | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_impatient_duplicate_phrasing | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_no_availability | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_out_of_scope_hotel | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_sold_out_flight | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_book_cheapest_from_search | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_cancel_by_reference | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_cancel_then_rebook | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_city_names_to_iata | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_date_written_out | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_iata_direct_then_book | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_max_price_filter | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_multiturn_departure_time | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_nonstop_and_budget_then_book | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_nonstop_flag | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_passenger_name_hyphenated | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_price_cap_then_book | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_reverse_direction | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_second_cheapest | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_by_flight_id | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_earliest_departure | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_only_nonstop | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_then_cancel | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_existing_booking | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_one_of_two | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_then_confirm | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_multiturn_book_by_time | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_report_cheapest_price | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_and_book_cheapest | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_basic | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_multiple_options | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_then_decline | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_two_leg_searches | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_nonstop_none_available | flaky | 3/5 | 0/5 | p=0.0833 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_budget_excludes_all | stable-fail | 0/5 | 0/5 | p=1 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/real-56sol-candidate/cases/<case>/rep_k.json
