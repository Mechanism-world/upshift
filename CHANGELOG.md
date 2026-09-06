# Changelog

## v0.4.0-dev — unreleased

Framework agents, without reading a framework. If the failing request is built inside
pydantic-ai, litellm, LangChain, the Vercel AI SDK, the Claude Agent SDK or opencode, there is
nothing to lift into five adapter files — so record the wire instead. (36 of the 52 Anthropic
rescue cases closed `UNSUPPORTED_FRAMEWORK` for exactly this.)

- **`upshift capture`** — a local forwarding recorder (stdlib only) that writes down the
  `/v1/messages` requests your agent really sends. Loopback-only by default; upstream status
  and body relayed verbatim, a 400 included; credential *and* account-identifier headers
  recorded as present, never as a value; SSE relayed chunk by chunk and reassembled, so a
  streaming agent adapts like a non-streaming one; requests grouped into conversations by
  their `messages` array; `--sim` records against the bundled simulator for $0. Both
  `/v1/messages` and the bare `/messages` are accepted.
- **`upshift adapt --from-capture`** — the five files, built from the recorded bytes. No model
  call, no source file read. Checks are derived, never invented; thinking blocks never reach a
  case; the generated backend replays recorded tool results and fails honestly on arguments it
  never saw. `ATTRIBUTION.md` and `ADAPT_EDITS.md` name every source and every deviation.
- **Framework mapping** — an accepted repair is now reported against the knob that expresses
  it in the framework the agent was captured from, with the file and line each mapping was
  verified at, for eight frameworks (`docs/framework-mapping.md`). In `REPORT.md`, in the
  terminal, and as a comment block above the first `diff --git` line of `upgrade.patch`. A
  knob a framework does not have is reported as "not mapped", never guessed.
- **`agent.json` `volatile_suffix`** — one recorded sample of a per-request block some agents
  regenerate on every call (a live facts block), appended at request-building time and never
  accumulated in the history.
- **`agent.json` `terminal_tools`** — tools whose call ends the episode, derived from the
  capture (a `tool_use` no later request ever answered). pydantic-ai's `final_result` is the
  canonical case; without this the replay hands the model a result the framework never
  produced and the model calls the tool again, failing the case on the *baseline* model.
- Live smoke: a real pydantic-ai agent, `claude-fable-5` → `claude-fable-5-1`, captured and
  upgraded end to end — `SAFE WITH PATCH`, 3/3 restored, $0.99
  ([reports/capture-pydantic-ai-smoke.md](reports/capture-pydantic-ai-smoke.md)). A smoke at
  N=3, explicitly not evidence.

## v0.3.1 — 2026-09-03

Pre-launch security pass over `adapt`, the runs root and the shell sandbox. Hostile input
here means a repository you point `upshift adapt` at, and the model output it steers.

- `adapt` refuses a source that git would read as an option (`--upload-pack=…/x.git` and
  friends) and clones with `--` terminating option parsing.
- The repo walk skips any path that resolves outside the repository root, so a checked-in
  symlink can no longer pull `~/.ssh/id_rsa` or `/etc/passwd` into the evidence sent to the
  model. (The pointer-following round already resolved paths against the root.)
- The generated `backend.py` escapes everything interpolated into its header docstring —
  origin, commit, and the model-written tool names and citations — and the rendered file is
  parsed before it is written. A repo whose text contains `"""` can no longer place
  statements in a file `upshift upgrade` imports and runs.
- `--tag` and case ids are validated as single directory names, so everything upshift writes
  (and everything the repair loop replaces) stays under the runs root.
- shell_gpt sandbox: added `--memory 512m` and `--security-opt no-new-privileges`.
- A hostile integer literal in a target repo no longer aborts `adapt` with an uncaught
  `ValueError` from CPython's int-to-string digit limit.
- New `tests/test_security.py` pins all of the above; `pip-audit` clean.

## v0.3.0 — 2026-09-02

- Second provider: Anthropic Messages API (`--provider anthropic`, endpoint `messages`),
  same statistics and repair loop, no provider forks in the core. Prompt caching on the
  cached prefix; cache-write pricing; identity-linked keys via `ANTHROPIC_WORKSPACE_ID`.
- Anthropic's documented Fable 5 → 5.1 changes as detectors and repairs: forced
  `tool_choice` 400 (remove + instruction), thinking-block invalidation (detect, refuse
  with the documented pointer), serialized tool calls (`turns_at_most` + the documented
  batching sentence), reduced retrieval at low effort (effort ladder + documented nudge),
  unsupported sampling params (drop). Effort calibration is a first-class repair.
- `sim-fable-5` / `sim-fable-5-1` for keyless rehearsal; `upshift adapt` reads Anthropic
  call sites and Jupyter notebooks.
- Release-day report on four open-source Claude agents (reports/fable-5-1-upgrade.md).
- Runner aborts on billing/quota errors instead of recording junk reps.

## v0.2.1 — 2026-09-01

- MVP readiness: package metadata (PyPI-ready), `upshift --version`, CI on Linux + macOS
  (lint, tests, wheel build, clean-environment tool install + sim demo), CHANGELOG.
- shell_gpt upstream issue filed with the verified fix: TheR1D/shell_gpt#801.

## v0.2.0 — 2026-09-01

- `upshift adapt <path-or-git-url>`: generate the five-file adapter directory from an
  agent codebase — static ranking + AST call-site analysis, model-as-extraction-engine
  over cited evidence, mechanical verbatim-citation gate (unverifiable text is omitted and
  reported, never written), round-2 extraction that follows the model's own pointers,
  every extraction call recorded and priced. Confidence per artifact derives only from
  the verification gate.
- Evaluated live on three unwritten repos (reports/adapt-*.md): shell_gpt zero-edit
  pipeline pass; HolmesGPT scaffolding with the prompt honestly deferred; ChatDBG honest
  refusal on docstring-buried schemas.

## v0.1.0 — 2026-08-29

- Public release. Installable via `uv tool install` / `pipx`; `upshift init`; packaged
  example agent; deterministic simulator for a keyless full-pipeline demo.
- ADAPTER.md contract; machinery made agent-agnostic.
- Real gpt-5.5 → gpt-5.6-sol results, records committed: 38-case booking agent 32/36
  restored with zero confirmed collateral (STAY PINNED); shell_gpt 14/14 restored by a
  one-line endpoint patch (SAFE WITH PATCH).
