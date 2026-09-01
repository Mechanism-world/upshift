# Upgrading a real OSS agent: shell_gpt on gpt-5.5 → gpt-5.6-sol

This is upshift's first proof run on an agent we didn't write:
[shell_gpt](https://github.com/TheR1D/shell_gpt) (12,263 stars, ~16.5k PyPI downloads/month,
MIT), adapted through the [ADAPTER.md](../ADAPTER.md) contract. Every claim below is backed
by committed run records under [`runs/`](../runs/) with the `shellgpt-56sol` prefix.

## Why shell_gpt

Its default-role agent calls `/v1/chat/completions` with one function tool
(`execute_shell_command`), `OPENAI_USE_FUNCTIONS=true` by default — and it has **no escape
hatch** from the documented gpt-5.6-family break (function tools rejected on
chat/completions unless `reasoning_effort` is `"none"` or the call moves to
`/v1/responses`): there is no `reasoning_effort` config key (the PR proposing one was
closed unmerged), no responses routing, and its optional litellm dependency is pinned
fourteen minor versions before litellm's fix. A user who bumps `DEFAULT_MODEL` from
`gpt-5.5` to `gpt-5.6-sol` — a documented one-line config change — gets a hard 400 on every
tool call. As of this run, no issue for this is filed on the shell_gpt repo.

## What we adapted, and exactly how honest it is

The prompt and tool schema were **generated from shell_gpt's own code** at upstream commit
`a082bd5` (not transcribed): `Function.openai_schema()` executed as-is, the role template
rendered through shell_gpt's own `OS_NAME`/`SHELL_NAME` config mechanism as Linux/bash.
Full provenance in [`agents/shell_gpt/ATTRIBUTION.md`](../agents/shell_gpt/ATTRIBUTION.md).

Tool execution runs shell_gpt's command in a network-isolated Docker container
(`--network none`, pinned mtimes, `TZ=UTC`, no jq, 30s timeout) over per-case fixture
trees, with the same `Exit code: N, Output:` envelope shell_gpt's tool returns. Known
deltas from the real runtime, all deliberate: our agent loop JSON-wraps the tool string
(identical bytes inside), responses are not streamed, turns cap at 6 (upstream recurses
unbounded), and `tool_choice`/`parallel_tool_calls` are not sent. None of these affect
whether the API accepts the request or which commands the model runs.

The 14 eval cases are grounded in shell_gpt's own README usage examples: 8 read-only
queries, 2 file-writing tasks, 2 identifier-fidelity cases, and 2 over-acting guards
(a messy directory *described*, not a cleanup *requested* — the check is that nothing gets
deleted). Numeric answers are asserted as standalone tokens with a build-time guard that
the expected number appears in no filename, file body, or file size — a case a model could
pass by pasting a directory listing would be a lie.

## Baseline: gpt-5.5

All 14 cases PASS: 69 of 70 reps passed (13 cases at 5/5, `read_json_email_field` at 4/5 —
above the 0.8 threshold). shell_gpt on gpt-5.5 does what its README says it does.
Records: `runs/shellgpt-56sol-baseline/`.

## The break: gpt-5.6-sol, unpatched

**0 of 70 reps passed. All 14 cases regressed** (one-sided Fisher exact p ≈ 0.004 per
case), every one with the same failure signature: the API rejects the request outright —

> Function tools with reasoning_effort are not supported for gpt-5.6-sol in
> /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort
> to 'none'.

shell_gpt never sets `reasoning_effort`; the 400 fires anyway on the 5.6 family. For a
shell_gpt user this is total loss of function-calling — and since rejected requests bill
zero tokens, the entire unpatched candidate run cost $0.00.
Records: `runs/shellgpt-56sol-candidate/`.

## The repair loop

Candidate 1 of 6, `route-to-responses` — a one-line change of `endpoint` from
`chat_completions` to `responses` in `agent.json`:

- Screen on the 14 broken cases: 14/14 restored.
- Full-suite verification: 14/14 cases at 5/5.
- Combined evidence per restored case: 10/10 reps (screen + verify, upshift's 2N
  acceptance criterion — a lucky single-run pass never counts as restored).

Accepted. No further candidates needed. Zero previously-passing cases broken, zero
relapses, zero flaky degradations. The behavioral regressions we braced for from the
booking-agent experiment (interrogation, identifier reformatting, over-acting) did not
materialize on this suite once the endpoint was routed: the two identifier-fidelity cases
and both over-acting guards passed every rep on patched 5.6-sol. Patched 5.6-sol was
140/140 on reps where 5.5 was 69/70 — we note that without claiming it means 5.6 is
better; at this sample size it doesn't.

## Verdict

**SAFE WITH PATCH.** The patch is [`runs/shellgpt-56sol/upgrade.patch`](../runs/shellgpt-56sol/upgrade.patch)
— apply with `git apply`. In shell_gpt's own codebase the equivalent fix is moving
`sgpt/handlers/handler.py` from `client.chat.completions.create` to the Responses API
(its `openai >= 2.0` dependency already supports it). Filed upstream with these records:
[TheR1D/shell_gpt#801](https://github.com/TheR1D/shell_gpt/issues/801) (2026-09-01); the
literal `sgpt` reproduction against PyPI release 1.5.1 was confirmed before filing.

This is the same machinery that returned **STAY PINNED** on our 38-case booking agent
(32/36 restored, two repairs vetoed on confirmed collateral damage). The verdict is not a
cheerleader: it says SAFE WITH PATCH when everything is provably restored, and STAY PINNED
when it isn't.

## What this cost

$0.56 of OpenAI API spend, total, on the flex tier: $0.27 baseline, $0.00 unpatched
candidate (all requests rejected, rejected requests are free), $0.29 screen + verify.
108k input / 27k output tokens across 210 recorded episodes. `upshift cost` reproduces
these numbers from the committed records.

## Limits of this run

- One agent, one tool, 14 cases. The single-tool surface means regressions show up as
  command quality, interrogation, and over-acting — not wrong-tool selection.
- The eval cases are ours, not the maintainer's; we kept them to behaviors the README
  itself advertises, but a maintainer might weight things differently.
- Sandboxed Linux/bash execution, not the user's own machine and shell.
