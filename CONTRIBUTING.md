# Contributing to upshift

Thanks for looking. upshift is small and opinionated: it decides whether a model upgrade
breaks your agent, and if it can, fixes it. Contributions that make that verdict more
trustworthy are the ones that land fastest.


## Dev setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Mechanism-world/upshift
cd upshift
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
```

The test suite is offline and makes zero API calls. The Docker-backed `shell_gpt` sandbox
tests self-skip when the `upshift-shellbox` image is absent; to run them:

```bash
docker build -t upshift-shellbox:latest \
  -f agents/shell_gpt/shellbox.Dockerfile agents/shell_gpt/
```

**macOS note.** uv's editable-install `.pth` file sometimes gets the `UF_HIDDEN` flag, which
makes CPython skip it, so `uv run upshift` fails with `import upshift`. `tests/conftest.py`
self-heals for the test suite; for the CLI entry point run:

```bash
chflags nohidden .venv/lib/python3.12/site-packages/*.pth
```

To try the tool without spending anything, everything works against the deterministic
simulator:

```bash
uv run upshift init demo-agent
uv run upshift upgrade --agent demo-agent --provider sim \
  --baseline-model sim-5.5 --candidate-model sim-5.6-sol --tag demo
```

## The evidence rule

This is the one rule that is not negotiable, because the whole project rests on it.

- **Run records committed to this repository must come from real API runs.** The `runs/`
  tree is public experimental evidence. Every record carries the provider it came from, and
  reports state it.
- **Simulator results are never evidence about a model.** `--provider sim` exists to
  exercise the machinery for free — it validates the pipeline, never the thesis. A PR may
  absolutely use sim runs to show that code works; it may not present a sim result as a
  finding about how a model behaves, and sim records do not belong in `runs/` as proof.
- **Do not lower N or loosen thresholds to make a case pass.** If a case is flaky, say so and
  raise reps; do not tune the statistics to the answer you wanted.
- If you commit a run record, **review it first** — records contain full prompts and
  responses. See [SECURITY.md](SECURITY.md).

## Adding an agent

An "agent" here is a five-file directory: `agent.json`, `system_prompt.txt`, `tools.json`,
`backend.py`, `cases/cases.json`. The contract — every field, every check type, the
determinism requirements a backend must satisfy — is in
**[ADAPTER.md](ADAPTER.md)**. Read it before writing one.

`upshift adapt <repo-or-url> --out <dir>` drafts the five files from an existing repository;
the draft is a starting point you are expected to read and correct, not output you should
trust.

If the agent is adapted from someone else's project, it needs an `ATTRIBUTION.md` in its
directory recording the upstream URL, the pinned commit, the license, which upstream file
every artifact came from, and every deviation from the real runtime — the existing
directories under `agents/` are the model to follow. Add the entry to
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) too, with the license text copied from that
project's own `LICENSE` file. Never state a license from memory.

## Adding a provider

Providers are transport-only: they take a fully-built request body and return the verbatim
response as a plain dict. They must not interpret the conversation, and endpoint semantics
belong in `agent_loop.py`, not in a provider fork. The design constraints, the endpoint
model, and the parameter-mapping rules are in **[DESIGN.md](DESIGN.md)**; the Anthropic
provider (added in v0.3) is the worked example of adding a second one.

## Pull requests

- One change per PR, with a title that says what changed.
- `uv run pytest -q` and `uv run ruff check src tests` pass. CI runs both on Ubuntu and
  macOS, builds the wheel, and audits dependencies with `pip-audit`.
- New behaviour comes with a test. Bug fixes come with a test that fails before the fix.
- If you changed something a document asserts (ADAPTER.md, DESIGN.md, README.md), update it
  in the same PR.
- Say what you actually verified and what you did not. An honest "I could not test this
  against a real model" is worth more than a confident guess — that norm is the whole
  project.
- No API keys, `.env` files, or credentials in a diff, ever.

There is no CLA and no sign-off ceremony. Open the PR.

## Licensing of contributions

upshift is MIT-licensed (see [LICENSE](LICENSE)). **Inbound = outbound:** contributions you
submit are licensed under the same MIT License as the project. If you include material you
did not write, say where it came from and under which license, and add it to
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Questions and bug reports

- Something is broken: [open a bug report](https://github.com/Mechanism-world/upshift/issues/new?template=bug_report.md).
- `upshift adapt` got your agent wrong: [that report](https://github.com/Mechanism-world/upshift/issues/new?template=agent_report.md)
  is the single most useful thing you can send us right now.
- Open-ended questions and ideas: [Discussions](https://github.com/Mechanism-world/upshift/discussions).
- Security issues: **not** a public issue — see [SECURITY.md](SECURITY.md).
