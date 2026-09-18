# upshift diff

`sim-5.5 @ chat_completions`  ->  `sim-5.6-sol @ chat_completions`

- provider: sim
- n_reps: 3
- baseline run: `verify-patch-adapter-sim-baseline`
- candidate run: `verify-patch-adapter-sim-candidate`

**SIMULATED PROVIDER - machinery validation only, not evidence about real models**

## Verdict: STAY PINNED

reason: 1 of 37 regressed cases still fail after the repair budget

still regressed: edge_ambiguous_missing_date, edge_book_unknown_flight, edge_budget_excludes_all, edge_cancel_already_cancelled, edge_cancel_nonexistent, edge_impatient_duplicate_phrasing, edge_no_availability, edge_nonstop_none_available, edge_out_of_scope_hotel, edge_sold_out_flight, exact_book_cheapest_from_search, exact_cancel_by_reference

at N=3 reps per case, NO per-case degradation reaches p<0.05 on a one-sided Fisher exact test — this run could not have detected even a total collapse. A non-significant difference is not evidence of equivalence.

collateral protection was not exercised on this run: no case passed on the candidate before repair.

Verification scope: adapted_agent

the adapter's backend.py executed real tool semantics; this proves the behaviour of the adapted reconstruction of the agent, not of the application's own code path.

This upgrade is NOT verified in the application: nothing here ran the application's own entry point.

evidence ids: verify-patch-adapter-sim-baseline=121bf5212f08  verify-patch-adapter-sim-candidate=dd023a22d9fd

the verdict rests on the FRESH final verification run `verify-patch-adapter-sim-final` (full suite, new seeds); the 21 screening/verification run(s) that SELECTED the candidates are listed as selection_runs and are not the evidence for them.

repair log:

- repair start: 37 regressed case(s), 0 protected passing case(s), budget 24 candidates
- candidate 1/24: [endpoint_routing] route-to-responses — Route API calls from /v1/chat/completions to /v1/responses (function tools + reasoning_effort are rejected on chat/completions for this model family).
-   screen route-to-responses: 18/37 broken cases restored
- candidate 2/24: [model_params] reasoning-effort-none — Set reasoning_effort='none' to keep function tools working on /v1/chat/completions (documented alternative to endpoint routing).
-   screen reasoning-effort-none: 18/37 broken cases restored
-   verification order (most restored, then no disclosed change, then playbook rank): route-to-responses (18/37), reasoning-effort-none (18/37, discloses a changed guarantee or cost)
-   ACCEPTED route-to-responses: restored ['edge_ambiguous_missing_date', 'edge_book_unknown_flight', 'edge_budget_excludes_all', 'edge_cancel_already_cancelled', 'edge_cancel_nonexistent', 'edge_no_availability', 'edge_nonstop_none_available', 'edge_out_of_scope_hotel', 'edge_sold_out_flight', 'exact_city_names_to_iata', 'exact_date_written_out', 'exact_max_price_filter', 'exact_nonstop_flag', 'exact_reverse_direction', 'happy_cancel_then_confirm', 'happy_search_multiple_options', 'happy_search_then_decline', 'happy_two_leg_searches']; 0 previously-passing cases broken; 19 regressed case(s) remain
- candidate 3/24: [prompt_edit] prompt-execution-discipline — Append an execution-discipline block: each tool at most once per request, never repeat a successful call.
-   screen prompt-execution-discipline: 10/19 broken cases restored
- candidate 4/24: [tool_schema_edit] tool-schema-book-once — Strengthen the book_flight description: exactly once per confirmed itinerary (skipped when the agent has no such tool).
-   screen tool-schema-book-once: 14/19 broken cases restored
- candidate 5/24: [prompt_edit] prompt-stop-after-goal — Append a stop-after-goal block: no further tool calls once the goal is achieved.
-   screen prompt-stop-after-goal: 4/19 broken cases restored
- candidate 6/24: [prompt_edit] prompt-no-fabrication — Append a no-fabrication block: never state a confirmation number a tool did not return; call the tool instead.
-   screen prompt-no-fabrication: 4/19 broken cases restored
- candidate 7/24: [model_params] raise-effort-one-rung — Raise reasoning_effort one rung to 'high' on the responses ladder (effort is only ever raised, never lowered).
-   screen raise-effort-one-rung: 0/19 broken cases restored
- candidate 8/24: [prompt_edit] prompt-verification-nudge — Append the documented verification nudge: verify unfamiliar names by searching before answering.
-   screen prompt-verification-nudge: 0/19 broken cases restored
- candidate 9/24: [prompt_edit] prompt-execute-dont-ask — Append an execute-don't-interrogate block: when a request already contains everything a tool needs, call it instead of asking for details the tool does not require (observed on real gpt-5.6-sol).
-   screen prompt-execute-dont-ask: 0/19 broken cases restored
- candidate 10/24: [prompt_edit] prompt-ground-in-results — Append a results-grounding block: never claim nothing is available when search returned flights (observed on real gpt-5.6-sol).
-   screen prompt-ground-in-results: 0/19 broken cases restored
-   verification order (most restored, then no disclosed change, then playbook rank): tool-schema-book-once (14/19), prompt-execution-discipline (10/19), prompt-stop-after-goal (4/19), prompt-no-fabrication (4/19)
-   ACCEPTED tool-schema-book-once: restored ['edge_impatient_duplicate_phrasing', 'exact_book_cheapest_from_search', 'exact_cancel_then_rebook', 'exact_iata_direct_then_book', 'exact_multiturn_departure_time', 'exact_nonstop_and_budget_then_book', 'exact_passenger_name_hyphenated', 'exact_price_cap_then_book', 'exact_second_cheapest', 'happy_book_earliest_departure', 'happy_book_only_nonstop', 'happy_book_then_cancel', 'happy_multiturn_book_by_time', 'happy_search_and_book_cheapest']; 0 previously-passing cases broken; 5 regressed case(s) remain
- candidate 11/24: [prompt_edit] prompt-execution-discipline — Append an execution-discipline block: each tool at most once per request, never repeat a successful call.
-   screen prompt-execution-discipline: 0/5 broken cases restored
- candidate 12/24: [prompt_edit] prompt-stop-after-goal — Append a stop-after-goal block: no further tool calls once the goal is achieved.
-   screen prompt-stop-after-goal: 4/5 broken cases restored
- candidate 13/24: [model_params] raise-effort-one-rung — Raise reasoning_effort one rung to 'high' on the responses ladder (effort is only ever raised, never lowered).
-   screen raise-effort-one-rung: 0/5 broken cases restored
- candidate 14/24: [prompt_edit] prompt-verification-nudge — Append the documented verification nudge: verify unfamiliar names by searching before answering.
-   screen prompt-verification-nudge: 0/5 broken cases restored
- candidate 15/24: [prompt_edit] prompt-execute-dont-ask — Append an execute-don't-interrogate block: when a request already contains everything a tool needs, call it instead of asking for details the tool does not require (observed on real gpt-5.6-sol).
-   screen prompt-execute-dont-ask: 0/5 broken cases restored
- candidate 16/24: [prompt_edit] prompt-ground-in-results — Append a results-grounding block: never claim nothing is available when search returned flights (observed on real gpt-5.6-sol).
-   screen prompt-ground-in-results: 0/5 broken cases restored
-   ACCEPTED prompt-stop-after-goal: restored ['exact_cancel_by_reference', 'happy_book_by_flight_id', 'happy_cancel_existing_booking', 'happy_cancel_one_of_two']; 0 previously-passing cases broken; 1 regressed case(s) remain
- candidate 17/24: [model_params] raise-effort-one-rung — Raise reasoning_effort one rung to 'high' on the responses ladder (effort is only ever raised, never lowered).
-   screen raise-effort-one-rung: 0/1 broken cases restored
- candidate 18/24: [prompt_edit] prompt-verification-nudge — Append the documented verification nudge: verify unfamiliar names by searching before answering.
-   screen prompt-verification-nudge: 0/1 broken cases restored
-   no screened candidate restored a broken case — none earned a full verification run
- all current candidates rejected; giving up
- final verification verify-patch-adapter-sim-final: full suite, 3 reps, fresh seeds — 36/38 cases pass
- repair end: 36/37 regressed cases restored; unrestored: ['happy_search_basic']

## Summary

- baseline: 37/38 cases pass (97.4%, CI 86.5-99.5%)
- candidate: 0/38 cases pass (0.0%, CI 0.0-9.2%)

37 regressed · 1 stable-fail

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| edge_ambiguous_missing_date | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_book_unknown_flight | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_budget_excludes_all | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_cancel_already_cancelled | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_cancel_nonexistent | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_impatient_duplicate_phrasing | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_no_availability | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_nonstop_none_available | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_out_of_scope_hotel | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| edge_sold_out_flight | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_book_cheapest_from_search | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_cancel_by_reference | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_cancel_then_rebook | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_city_names_to_iata | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_date_written_out | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_iata_direct_then_book | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_max_price_filter | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_multiturn_departure_time | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_nonstop_and_budget_then_book | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_nonstop_flag | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_passenger_name_hyphenated | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_price_cap_then_book | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_reverse_direction | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| exact_second_cheapest | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_by_flight_id | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_earliest_departure | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_only_nonstop | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_book_then_cancel | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_existing_booking | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_one_of_two | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_cancel_then_confirm | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_multiturn_book_by_time | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_and_book_cheapest | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_basic | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_multiple_options | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_search_then_decline | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_two_leg_searches | regressed | 3/3 | 0/3 | p=0.05 * | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |
| happy_report_cheapest_price | stable-fail | 1/3 | 0/3 | p=0.5 | api_error_tools_reasoning | API call failed: Function tools with reasoning_effort are... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

38 test(s) performed, one per case; p is NOT adjusted for multiple comparisons, so at alpha=0.05 roughly 2 of 38 could reach significance by chance alone. Read a single starred p as a pointer to a transcript, never as a result on its own.

full transcripts: runs/verify-patch-adapter-sim-candidate/cases/<case>/rep_k.json
