# upshift

CLI that makes upgrading a production AI agent to a new model version safe. Input: an agent + eval cases + a candidate model version. Output: behavioral diff, attempted repairs, a git diff patch, and a verdict (SAFE / SAFE WITH PATCH / STAY PINNED).

## Why this exists

Model/SDK updates silently break production agents (tool-calling failures, param semantics changes, over-acting). Detection tools exist; repair doesn't. We fix the upgrade or prove you should stay pinned.

## Architecture decisions

- Python, uv for env, single package
- v0 targets plain OpenAI API agents (chat/completions and responses), no framework integration yet
- Everything runs locally with the user's API keys, nothing leaves their machine
- Every eval case runs N times (default 5) against both model versions; pass/fail by threshold, never a single run
- Repair loop: generate candidate fix → re-run affected cases N times → accept only if broken cases pass AND previously passing cases still pass → else next candidate or give up
- Repairs limited to: prompt edits, model params (reasoning effort, temperature), tool schema edits, endpoint routing. Nothing else in v1.
- Deterministic, resumable runs: every run recorded to disk (inputs, outputs, versions, params) so any diff is inspectable later

## Session log

(append two lines per session: what was built, what was decided)

2026-08-27: Built the full v1 pipeline — victim booking agent + 38 checked cases, runner/recorder (N-rep, resumable), Fisher/Wilson differ, signature-driven repair loop, git-diff patch, verdict CLI; validated end-to-end on the deterministic simulator (SAFE WITH PATCH, 36/36 restored, 0 broken).
Decided: deterministic checks only (no LLM judge), sim results never count as evidence (provider tag enforced in reports), repair candidates stack only if full-suite verify shows zero collateral damage; real gpt-5.5→5.6-sol run blocked on OpenAI credits (429 no-credits), machinery ready.

2026-08-28: Ran the real 5.5→5.6-sol experiment (flex tier + caching, $12.31 total): 36/38 cases regressed (hard 400 on chat+tools), repair stack (endpoint routing + execute-don't-ask prompt + reasoning-high) restored 32/36 = 88.9% with zero confirmed collateral; verdict STAY PINNED (4 unrestored: 1 brittle-assertion artifact, 1 stubborn interrogation, 2 identifier-reformatting vetoed by confirmed tradeoff).
Decided: contested case statuses adjudicated on 2N reps at unchanged thresholds (single-sample vetoes were firing on borderline-flaky cases); no candidate retries after confirmed rejection (p-hacking) and no phrasing-dictation prompt repairs (eval overfitting); THE number is 88.9%.
