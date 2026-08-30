# upshift

Test whether a model upgrade breaks your agent — and either fix it or prove you should stay pinned.

Input: a plain OpenAI tool-calling agent, its eval cases, and a candidate model version.
Output: a statistical behavioral diff, an attempted automated repair, a `git apply`-able patch,
and one of three verdicts: **SAFE**, **SAFE WITH PATCH**, or **STAY PINNED**.

## The number

We ran a 38-case booking agent through the real `gpt-5.5` → `gpt-5.6-sol` upgrade
(all artifacts committed under [`runs/`](runs/), including every API request and response):

- **36 of 38 cases regressed.** Every tool-using case died on the documented hard 400
  (`gpt-5.6` family rejects function tools on `/v1/chat/completions`), and behavioral
  regressions followed once the 400 was routed around.
- **The repair loop restored 32 of 36 (88.9%) with zero confirmed collateral damage** —
  three stacked repairs, each verified on the full suite: route to `/v1/responses` (+26),
  an execute-don't-interrogate prompt block (+5), `reasoning_effort: high` (+1).
- **The verdict was still STAY PINNED.** That is the tool working as designed: an upgrade is
  blessed only when *every* regression is repaired and *nothing* previously passing breaks.
  Four cases stayed broken — one of them turned out to be a brittle eval assertion on our
  side, one is a stubborn interrogation behavior, and two are identifier-reformatting cases
  whose candidate fix measurably harmed an already-restored case and was rejected on that
  evidence. The full accounting is in [`runs/real-56sol/REPORT.md`](runs/real-56sol/REPORT.md).

Total API cost of the experiment: $12.31 (flex tier + prompt caching).

We then pointed the same pipeline at an agent we didn't write:
[shell_gpt](https://github.com/TheR1D/shell_gpt) (12k stars), which the same upgrade
hard-breaks — 14/14 cases regressed on the documented 400, with no workaround available in
its config. One accepted repair (a one-line endpoint route to `/v1/responses`) restored
14/14 with zero collateral: **SAFE WITH PATCH**, verified on 10 reps per case, for $0.56 of
API spend. Full write-up: [reports/shellgpt-upgrade.md](reports/shellgpt-upgrade.md).

## Quickstart

Sixty seconds, no API key — the built-in deterministic simulator runs the full pipeline:

```bash
uv tool install git+https://github.com/atilavahedian/upshift   # or: pipx install ...
upshift init my-agent
upshift upgrade --agent my-agent --provider sim \
  --baseline-model sim-5.5 --candidate-model sim-5.6-sol --tag demo
```

You'll watch a baseline run, a candidate run that regresses 36/38 cases, a repair loop that
restores all of them, a **SAFE WITH PATCH** verdict, and a `git apply`-able patch — in about
a second, for $0. (Simulator results validate the machinery, never a real model: every
report is tagged with its provider, and sim evidence can't produce a real verdict.)

For a real upgrade, put `OPENAI_API_KEY` in your environment (or a local `.env`), point
upshift at your own agent directory (see [ADAPTER.md](ADAPTER.md)), and run:

```bash
upshift upgrade --agent my-agent --flex \
  --baseline-model gpt-5.5 --candidate-model gpt-5.6-sol --tag my-upgrade
```

`--flex` uses OpenAI's flex service tier (~50% cheaper, stacks with prompt caching).
Every case runs N=5 times per model; `upshift cost` prints the exact recorded spend.
Runs are resumable — Ctrl-C exits immediately and a rerun picks up where it stopped.

## How it works (and why single runs lie)

Detection tools tell you *that* an upgrade broke your agent. upshift is one step downstream:
it tries to fix the break, and gives you the evidence either way.

- **N repetitions, never single runs.** Every case runs N times (default 5) against both
  model versions. A case's outcome is its pass rate against fixed thresholds (≥ 0.8 PASS,
  ≤ 0.4 FAIL, in between FLAKY) — because agents are stochastic and a single run of a
  borderline case is a coin flip wearing a lab coat.
- **Five labels, with p-values.** Each case is labeled stable-pass, stable-fail, regressed,
  improved, or flaky, and every label ships with a one-sided Fisher exact test on the pass
  counts so you can judge the strength of the evidence yourself. Suite-level rates carry
  Wilson 95% intervals. Flaky is never silently promoted to regressed or improved.
- **Deterministic checks only.** Pass/fail comes from tool-call assertions, final backend
  state, and response content checks — no LLM judge in the loop, so the statistics mean
  what they say.
- **Repairs are screened, verified, and adjudicated.** A repair candidate (prompt edit,
  model params, tool schema edit, or endpoint routing — nothing else) is first screened on
  the broken cases, then verified on the FULL suite. It is accepted only if it restores at
  least one broken case, breaks zero previously-passing cases, and relapses nothing an
  earlier repair restored. Contested statuses are settled on 2N reps at unchanged
  thresholds — symmetric for restorations and vetoes, because "never a single run" applies
  to evidence *against* a patch too. We learned that the hard way: our first real run vetoed
  every behavioral repair off single-sample flips of one borderline case (4/5, 3/5, 3/5,
  4/5 across four runs, patch or no patch).
- **No p-hacking by construction.** A candidate rejected on confirmed evidence is never
  retried, and the playbook contains no repairs that dictate exact output phrasings —
  restoring an eval by overfitting to its assertions would make the number a lie.
- **Everything is recorded.** Every run writes its manifest, every API request and response
  verbatim, every tool execution, every check result to `runs/<run_id>/`. Runs are
  resumable; the records are the evidence behind the verdict, and this repo's own claims
  are backed by its committed records.

## Plugging in your agent

Your agent is five files in a directory — an `agent.json`, the system prompt, the OpenAI
tool schemas, a deterministic `backend.py` that executes tool calls locally, and your eval
cases. The full contract is ~20 lines: [ADAPTER.md](ADAPTER.md). `upshift init` scaffolds a
working example to start from. No framework integrations — plain OpenAI chat-completions
and responses agents only, by design.

## What leaves your machine: nothing

Your API key stays in your environment and is used only to call the OpenAI API directly.
Your prompts, tool schemas, transcripts, and eval results are written to your local `runs/`
directory and go nowhere else. There is no telemetry, no account, no server. Read the
source — it fits in an afternoon.

## Honest limits

- Tested on two agents so far: our synthetic booking agent (the committed experiment above)
  and one real open-source agent — [shell_gpt](https://github.com/TheR1D/shell_gpt), which
  the same upgrade hard-breaks and a verified one-line patch fully repairs
  (**SAFE WITH PATCH**, 14/14 restored, $0.56 of API spend:
  [the report](reports/shellgpt-upgrade.md)). That is not a benchmark suite.
- OpenAI API agents only (chat/completions and responses). No Anthropic/Google/local models
  yet, no framework integrations (LangChain, crewAI, etc.).
- Repairs are limited to prompt edits, model params, tool schema edits, and endpoint
  routing. The tool-schema repair currently only fires for one known tool shape; foreign
  agents effectively get three of the four repair types.
- The eval cases are yours to write, and the verdict is only as good as they are. Brittle
  assertions produce brittle verdicts — one of our own four unrestored cases was exactly
  that, and we say so in the report.
- Backends must be deterministic; upshift documents this requirement but cannot yet detect
  a nondeterministic backend — it will just surface as flakiness.
- The statistics are honest but small-N: N=5 with Fisher exact tests tells you a 5/5 → 0/5
  collapse is real (p ≈ 0.004); it will not resolve subtle single-case effects. Raise N if
  you need more power and can pay for it.

## Development

```bash
uv sync && uv run pytest -q && uv run ruff check src tests
```

macOS note: uv's editable-install `.pth` file sometimes gets the `UF_HIDDEN` flag, which
makes CPython skip it (`import upshift` fails from `uv run upshift`). Tests self-heal via
`tests/conftest.py`; for the CLI entry point run
`chflags nohidden .venv/lib/python3.12/site-packages/*.pth`.

## License

MIT.
