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

2026-08-29: Launch phase — made upshift installable (packaged example agent, `upshift init`, container-verified 5-min path, preflight errors, instant SIGINT+resume), agent-agnostic (ADAPTER.md contract, 9 genericity fixes, foreign toy agent through full sim pipeline), and adapted the first real OSS proof target: shell_gpt (12.3k stars, MIT) — prompt+schema generated from upstream a082bd5, docker-sandboxed backend, 14 cases, smoke gate 20/20 on real gpt-5.5 ($0.105).
Decided: shell_gpt over HolmesGPT/home-llm/ChatDBG/py-gpt because it alone is hard-broken on 5.6-sol with no user-side escape (hardcoded chat+tools, no reasoning_effort config, litellm pinned pre-fix); playbook prompt blocks rewritten domain-neutral (sim markers preserved, committed run records keep the as-sent text — foreign-agent safety beats byte-reproducibility); evidence runs/ stays public in the repo (70.9MB, packs to 3.5MiB); MIT license.

2026-08-28: Ran the real 5.5→5.6-sol experiment (flex tier + caching, $12.31 total): 36/38 cases regressed (hard 400 on chat+tools), repair stack (endpoint routing + execute-don't-ask prompt + reasoning-high) restored 32/36 = 88.9% with zero confirmed collateral; verdict STAY PINNED (4 unrestored: 1 brittle-assertion artifact, 1 stubborn interrogation, 2 identifier-reformatting vetoed by confirmed tradeoff).
Decided: contested case statuses adjudicated on 2N reps at unchanged thresholds (single-sample vetoes were firing on borderline-flaky cases); no candidate retries after confirmed rejection (p-hacking) and no phrasing-dictation prompt repairs (eval overfitting); THE number is 88.9%.
