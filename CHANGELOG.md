# Changelog

## Unreleased

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
