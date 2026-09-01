# `upshift adapt` evaluation 1/3: shell_gpt (ground truth available)

Target: [shell_gpt](https://github.com/TheR1D/shell_gpt) @ `a082bd5` — the same commit our
hand-built adapter ([agents/shell_gpt/](../agents/shell_gpt/)) was extracted from, which
took 12–15 hours of human work and served as ground truth for grading. Command:
`upshift adapt <clone> --out <dir> --flex`, no agent hint. Extraction model gpt-5.5.

## Result

| run | wall clock | extraction cost | outcome |
|---|---|---|---|
| 1 | 57s | $0.14 | contract-valid dir; **live bug found**: generated checks used `value` where the engine expects `text` — every check "could not be evaluated". Fixed in the tool (param normalization + validation), not in the output. |
| 2 | 66s | $0.15 | checks fixed; **live bug found**: multi-turn cases got one oracle terminator for two user messages, so the sim replayed "Done." as the final message. Fixed in the tool. |
| 3 | 57s | $0.09 (prompt cache hit) | **full upgrade pipeline runs on the generated directory with zero edits**: 4/4 generated cases pass the sim-5.5 baseline, 4/4 regress on sim-5.6-sol, repair restores 4/4 → SAFE WITH PATCH. |

Both live bugs became committed regression tests. That was the point of running against
ground truth first.

## Generated vs hand-built, field by field (run 3)

- **`agent.json` — high confidence, and the two load-bearing fields (endpoint
  `chat_completions`, model `gpt-5.5`) are exact.** The generated `params`
  (`temperature 0.0`, `top_p 1.0`, `tool_choice auto`, `parallel_tool_calls false`) are
  *more* faithful to upstream than the hand-built `{}` — sgpt really sends all four; the
  hand-built adapter documented dropping them as a deliberate delta. `max_turns` 12 vs our
  chosen 6 (upstream is unbounded; both are harness caps). Name cosmetic.
- **`system_prompt.txt` — medium.** Body of the default role is verbatim-correct. Two
  human decisions correctly surfaced instead of guessed: the `{os}`/`{shell}` placeholders
  are left unrendered with citations to where sgpt supplies them (we chose Linux/bash by
  hand), and the templated first line "You are ShellGPT" was omitted by the verification
  gate — it is rendered from `ROLE_TEMPLATE = "You are {name}"` and exists nowhere
  literally in source — with a must-review note saying exactly that. Wrong-but-confident
  was the failure mode to avoid; this is the uncertain-and-explicit behavior we wanted,
  at the cost of one line a human re-adds in seconds.
- **`tools.json` — medium.** `execute_shell_command` matches the hand-built schema
  including pydantic's nonstandard `title`/`example` keys. It also kept the macOS-only
  `execute_apple_script`, which we had dropped deliberately for a Linux sandbox — a
  judgment adapt cannot make for you.
- **`cases/cases.json` — medium.** 4 usable drafts grounded in cited README/test usage.
  Case quality varies between runs (extraction is stochastic), and one run mixed sgpt's
  chat and code modes — with multi-mode agents, pass `--agent-hint`.
- **`backend.py` — low, and honestly so.** `execute_shell_command` runs a real shell;
  adapt generates a stub with a TODO, not a fake. Our hand-built Docker-sandboxed backend
  — the bulk of the original 12–15 hours — is exactly the part that stays human work when
  an agent's tools touch the real world.

## Bottom line

Command to sim-validated adapter: **under a minute of compute, ~$0.14, zero human edits.**
To match the hand-built adapter used in the real paid experiment: ~4 small review edits
(render two placeholders, re-add one templated line, drop one macOS tool) *plus writing a
real deterministic backend for the shell tool* — the honest boundary of what adapt
automates. For agents whose tools are mechanical (lookups, state machines, file
fixtures), the generated spec-interpreter backend can be complete as-is; for tools that
touch the world, adapt hands you the contract and the TODO.
