# upshift diff

`claude-haiku-4-5-20251001 @ messages`  ->  `claude-sonnet-5 @ messages`

- provider: anthropic
- n_reps: 5
- baseline run: `opus47-001-baseline`
- candidate run: `opus47-001-patched-candidate`
- agent files differ between runs (patched candidate)

## Summary

- baseline: 5/5 cases pass (100.0%, CI 56.6-100.0%)
- candidate: 5/5 cases pass (100.0%, CI 56.6-100.0%)

5 stable-pass

## Cases that changed

5 stable-pass cases not listed.

No non-stable-pass cases.

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/opus47-001-patched-candidate/cases/<case>/rep_k.json
