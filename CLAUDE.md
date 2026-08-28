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
