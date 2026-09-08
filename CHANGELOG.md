# Changelog

## v0.5-dev — unreleased (reliability follow-ups)

Five findings from the OpenAI and Anthropic rescue tracks, each of which produced a wrong or
unsupported ANSWER rather than a crash. Sources are the private ops repo's case files.

- **The repair loop screens every sibling candidate before it accepts one** (`ghisdk-127`,
  `ghc-062`). Greedy acceptance let the playbook's emission order decide the verdict: waku
  accepted a candidate that restored 8 of 9 cases and published STAY PINNED while a sibling in
  the same generation restored all nine, and crispen shipped a repair with a disclosed change
  of guarantee ahead of the maintainer's own equivalent fix. Restorers are now ranked by full
  restoration, then cases restored, then the absence of a disclosure, then the playbook rank,
  and verified in that order. Screening costs one screen run per sibling and `--budget` still
  bounds the candidates TRIED, so its default is 24 rather than 6.
- **A candidate whose adjudication cannot run is rejected, not accepted** (`ghisdk-052`). When
  the cost ceiling stops the N reps that settle a contested case, the candidate is rejected,
  the suspects are recorded as `adjudication_skipped`, and the verdict is
  INCONCLUSIVE(cost_ceiling) — never SAFE WITH PATCH on the strength of the sample that raised
  the suspicion.
- **Transient provider failures are retried, and never recorded as behaviour** (`ghi56-019`,
  `ghi56-006`, `ghi56-021`). A flex capacity 429 was written down three times as a permanently
  failing rep, moving the pass rate the verdict is computed from. 429/5xx/timeouts/connection
  errors are now retried per rep (3 attempts, jittered exponential backoff, ≤90s) outside the
  SDK's own retries; an unrecovered one is recorded as `transient_provider_error`, which the
  differ files under `harness_error` and the verdict treats as INCONCLUSIVE. Billing still
  aborts, auth is not retried, and a 400 is never retried or reclassified. New
  **`--retry-errored`** on `run`/`upgrade` re-runs, on a resume, exactly the reps whose
  recorded error was non-behavioural.
- **`response_format` is carried, not deleted** (`ghisdk-051`). adapt used to drop it, which
  removed the entire subject of a structured-output incident and produced an agent that asked
  for no structured output at all. It is now a param, translated per endpoint: verbatim on
  chat/completions, `text.format` (with the json_schema object flattened) on `/v1/responses`,
  dropped with a recorded reason on Anthropic's messages. A nullable tool parameter also keeps
  its null branch (`ghisdk-052`): every spelling is canonicalised to `anyOf` with null.
- **A disclosed strict-schema repair** (`ghisdk-051`, `p2-001`). New signature
  `api_error_schema_invalid` and new candidate `schema-strict-compat:<schema>`, applying
  OpenAI's three documented strict rules to one named schema. Disclosed: optional fields become
  required-and-nullable, so downstream code must accept null.

## v0.4.1-dev — unreleased

Three fixes found by running capture mode on its first real case outside pydantic-ai
(rescue-ops `cases/A-075`, litellm 1.83.9). The first is a false pass, in the one direction
this product must never fail.

- **A capture-derived agent now replays the framework's per-turn shape.** `adapt
  --from-capture` wrote ONE episode-level `tool_choice`, picked by frequency over the recorded
  requests. A framework that forces a tool on turn 1 and then goes `auto` — pydantic-ai and
  litellm both do, and so does every structured-output and routing agent — became an adapter
  that forced a tool on *every* turn. Under a forced choice the model cannot answer in text,
  so the replayed episode called tools until `max_turns` and failed its own `turns_at_most`
  **on the baseline model**; with nothing passing on the baseline there was no regression to
  find, and the verdict read `SAFE`, "the candidate model is a drop-in replacement", over a
  suite that never worked once.
  - **`agent.json` `turn_params`** (ADAPTER.md): param overrides applied over `params` by
    assistant-turn index, the last entry repeating, `null` meaning the field was not sent on
    that turn. `agent_loop` applies turn N's overrides to turn N's request; the repair loop
    reads the sequence too, so removing a forced `tool_choice` removes it from every entry
    instead of finding nothing to repair.
  - **`adapt --from-capture` derives it** for exactly the params one recorded conversation
    sent differently across its turns, and leaves those out of `params`. Captures that
    disagree with each other in a way no sequence can express are **not** resolved quietly:
    every variant and its count land in `ADAPT_EDITS.md` under CONFLICTS, and
    **`adapt --from-capture --strict`** exits non-zero. A tie is broken by canonical text and
    reported — never by which request the recorder happened to see first, which is what
    `Counter.most_common` was doing.
- **`BASELINE_BROKEN`** — the guard that catches this independently of capture. A run whose
  baseline model passed **no** case measured nothing about the candidate and can never be
  `SAFE`. Checked before every other verdict, and unable to mask one (a regression needs a
  case the baseline passed). `upshift upgrade` also says it between the two legs, before the
  candidate run spends money on a comparison that cannot mean anything.
- **Captures, run records and adapt records are stamped with the version that actually ran.**
  `upshift.__version__` was a hand-maintained literal three releases behind `pyproject.toml`,
  so every one of them claimed `0.1.0` while `upshift --version` reported the truth. It now
  reads the installed package metadata, the way `cli._version()` always did. Provenance only:
  no measurement changes, and the 169 already-committed lab records carry the stale string.
- **Tool fields the adapter cannot carry are reported, not dropped in silence.** The
  chat-style shape holds `name`/`description`/`input_schema`; everything else a recorded tool
  carried is now a per-tool note in `ADAPT_EDITS.md`, louder for a server tool — a
  `computer_20241022` with no `input_schema` at all was becoming a plain custom tool with an
  empty schema, with nothing anywhere saying so. Structural deviation 2 stops claiming
  byte-identity and names the two fields dropped on purpose.

Also unreleased, from the OpenAI migration-rescue track (product gaps the lab hit while
running 54 cases):

- Endpoint routing translates the output-token cap. `/v1/responses` spells it
  `max_output_tokens`; an agent written against `/v1/chat/completions` carries `max_tokens`
  (classic families) or `max_completion_tokens` (gpt-5*/o-series), and passing either to the
  Responses SDK raises `TypeError: Responses.create() got an unexpected keyword argument
  'max_completion_tokens'` before a request is sent — crashing the whole run rather than
  recording a failed rep. Since endpoint routing is the documented repair for the gpt-5.5+ /
  gpt-5.6 "function tools ... in /v1/chat/completions" 400, the untranslated cap made that
  repair unusable for any agent that sets one. `map_params` now maps both spellings to
  `max_output_tokens` on `responses`; an explicitly-spelled `max_output_tokens` wins.

- Pricing for the rest of the gpt-5.6 family. `upshift cost` reported "unknown rate" for
  gpt-5.6-terra and gpt-5.6-luna. Standard-tier rates per 1M tokens, from
  https://developers.openai.com/api/docs/pricing (fetched 2026-09-03): sol $4.00 in /
  $0.40 cached / $20.00 out (unchanged), terra $2.00 / $0.20 / $12.00, luna $0.20 / $0.02 /
  $1.20. The published flex and batch rows are exactly half of standard and the cached rows
  exactly 10% of input, which is what the existing tier and cache multipliers already do.

- `upshift run` and `upshift upgrade` take `--max-cost-usd`. Only `adapt`, which makes one
  paid call, had a spend ceiling; `run` and `upgrade` make thousands — baseline reps,
  candidate reps, then a screen and a full-suite verify per repair candidate — and a lab
  overran a $5 per-case cap inside a single `upgrade`. The ceiling is priced, not estimated:
  it sums the recorded token usage under this run id (for `upgrade`, the whole `--tag`
  family) through the same `pricing` module `upshift cost` uses, and is checked before every
  rep is dispatched and again between phases and repair candidates. On reaching it the
  command stops before the next API call, leaves every completed rep on disk (rerun the same
  command with a higher ceiling to resume), prints the priced total and the phase that
  stopped, and exits 3 — distinct from a STAY PINNED verdict (1) and a usage error (2).
  No verdict is emitted, and a `COST_STOPPED.json` marker lands beside `diff.json` so a
  partial pipeline can never be read as a finished one; it is deleted when one finishes.
  Unpriced models fail closed: a model with no published rate warns loudly at startup and
  its usage is charged at the highest rate in the table, never at $0.

- Pricing for the gpt-5.2 family and gpt-5-mini. `upshift cost` reported "unknown rate" for
  every model outside the 5.5/5.6 families, and an unpriced leg is exactly what a spend
  ceiling must not treat as free. Standard-tier rates per 1M tokens, from
  https://developers.openai.com/api/docs/pricing (fetched 2026-09-03): gpt-5.2 $1.75 in /
  $0.175 cached / $14.00 out, gpt-5.2-pro $21.00 / — / $168.00, gpt-5-mini $0.25 / $0.025 /
  $2.00. gpt-5.2-pro is listed separately because longest-prefix matching would otherwise
  price a `-pro` run at the `gpt-5.2` rate and understate it twelvefold.

- `adapt` reconstructs prompts written as Python implicit string concatenation correctly.
  `("You are a screener. " "Return JSON with keys a, b.")` is one string to the interpreter,
  and adapt was inserting a newline between the two literals while labelling both verbatim —
  a generated agent that sends a prompt the upstream agent never sends. The gate now records
  each chunk's span inside the source literal it came from, and chunks that sit end to end
  inside one literal are joined with "" exactly as Python joins them; chunks from separate
  statements, from non-Python sources, or that cannot be placed keep the newline join.

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
