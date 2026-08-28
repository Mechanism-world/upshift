# upshift diff

`sim-5.5 @ chat_completions`  ->  `sim-5.6-sol @ chat_completions`

- provider: sim
- n_reps: 5
- baseline run: `sim-e2e-baseline`
- candidate run: `sim-e2e-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Verdict: SAFE WITH PATCH

restored 36/36 regressed, 0 previously-passing broken

patch: runs/sim-e2e/upgrade.patch

repair log:

- repair start: 36 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [endpoint_routing] route-to-responses — Route API calls from /v1/chat/completions to /v1/responses (function tools + reasoning_effort are rejected on chat/completions for this model family).
-   screen: 18/36 broken cases restored — running full verification
-   ACCEPTED: restored ['edge_ambiguous_missing_date', 'edge_book_unknown_flight', 'edge_budget_excludes_all', 'edge_cancel_already_cancelled', 'edge_cancel_nonexistent', 'edge_no_availability', 'edge_nonstop_none_available', 'edge_out_of_scope_hotel', 'edge_sold_out_flight', 'exact_city_names_to_iata', 'exact_date_written_out', 'exact_max_price_filter', 'exact_nonstop_flag', 'exact_reverse_direction', 'happy_cancel_then_confirm', 'happy_search_multiple_options', 'happy_search_then_decline', 'happy_two_leg_searches']; 0 previously-passing cases broken; 18 regressed case(s) remain
- candidate 2/6: [prompt_edit] prompt-execution-discipline — Append an execution-discipline block: each tool at most once per request, never repeat a successful call.
-   screen: 10/18 broken cases restored — running full verification
-   ACCEPTED: restored ['edge_impatient_duplicate_phrasing', 'exact_book_cheapest_from_search', 'exact_cancel_then_rebook', 'exact_multiturn_departure_time', 'exact_passenger_name_hyphenated', 'exact_price_cap_then_book', 'happy_book_earliest_departure', 'happy_book_then_cancel', 'happy_multiturn_book_by_time', 'happy_search_and_book_cheapest']; 0 previously-passing cases broken; 8 regressed case(s) remain
- candidate 3/6: [tool_schema_edit] tool-schema-book-once — Strengthen book_flight description: exactly once per confirmed itinerary.
-   screen: 4/8 broken cases restored — running full verification
-   ACCEPTED: restored ['exact_iata_direct_then_book', 'exact_nonstop_and_budget_then_book', 'exact_second_cheapest', 'happy_book_only_nonstop']; 0 previously-passing cases broken; 4 regressed case(s) remain
- candidate 4/6: [prompt_edit] prompt-stop-after-goal — Append a stop-after-goal block: no further tool calls once the goal is achieved.
-   screen: 4/4 broken cases restored — running full verification
-   ACCEPTED: restored ['exact_cancel_by_reference', 'happy_book_by_flight_id', 'happy_cancel_existing_booking', 'happy_cancel_one_of_two']; 0 previously-passing cases broken; 0 regressed case(s) remain
- repair end: all 36 regressed cases restored

## Summary

- baseline: 36/38 cases pass (94.7%, CI 82.7-98.5%)
- candidate: 0/38 cases pass (0.0%, CI 0.0-9.2%)

36 regressed · 2 flaky

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| edge_ambiguous_missing_date | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_book_unknown_flight | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_budget_excludes_all | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_cancel_already_cancelled | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_cancel_nonexistent | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_impatient_duplicate_phrasing | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_no_availability | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_nonstop_none_available | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
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
| happy_search_and_book_cheapest | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_multiple_options | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_then_decline | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_two_leg_searches | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_report_cheapest_price | flaky | 3/5 | 0/5 | p=0.0833 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_basic | flaky | 3/5 | 0/5 | p=0.0833 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/sim-e2e-candidate/cases/<case>/rep_k.json
