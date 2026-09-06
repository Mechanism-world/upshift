# upshift diff

`claude-haiku-4-5-20251001 @ messages`  ->  `claude-sonnet-5 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `opus47-001-baseline`
- candidate run: `opus47-001-candidate`

## Verdict: STAY PINNED

reason: 5 of 5 regressed cases still fail after the repair budget

still regressed: name_topic_economics, name_topic_education, name_topic_environment, name_topic_health, name_topic_technology

repair log:

- repair start: 5 regressed case(s), 0 protected passing case(s), budget 6 candidates
- no further candidates for the observed failure signatures; giving up
- repair end: 0/5 regressed cases restored; unrestored: ['name_topic_economics', 'name_topic_education', 'name_topic_environment', 'name_topic_health', 'name_topic_technology']

## Summary

- baseline: 5/5 cases pass (100.0%, CI 56.6-100.0%)
- candidate: 0/5 cases pass (0.0%, CI 0.0-43.4%)

5 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| name_topic_economics | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_unsupported_sampling_params | API call failed: `temperature` is deprecated for this mod... |
| name_topic_education | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_unsupported_sampling_params | API call failed: `temperature` is deprecated for this mod... |
| name_topic_environment | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_unsupported_sampling_params | API call failed: `temperature` is deprecated for this mod... |
| name_topic_health | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_unsupported_sampling_params | API call failed: `temperature` is deprecated for this mod... |
| name_topic_technology | regressed | 5/5 | 0/5 | p=0.00397 ** | api_error_unsupported_sampling_params | API call failed: `temperature` is deprecated for this mod... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/opus47-001-candidate/cases/<case>/rep_k.json
