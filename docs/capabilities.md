# Capabilities — what is implemented, and how far it has been tested

Three columns, three different claims. They are not the same claim, and a row that stops at
the first column is not a row you should bet a production migration on.

| column | what it means |
| --- | --- |
| **implemented + offline-tested** | The code path exists and is covered by tests that run with `uv run pytest -q`: simulator, scripted provider, or committed record. No network, no key, no money. It proves the code does what we think it does — not that a provider agrees. |
| **live-smoke-tested** | At least one real request was sent to the real API on this path and the wire format was confirmed. A smoke is a handful of cases at low N. It proves the path is real; it is not evidence about any model's behaviour. |
| **end-to-end migration verified** | A complete `upshift upgrade` ran on this path against two real model versions, with a committed run record and a verdict: baseline, candidate, repairs, full-suite verification. This is the only column that is evidence. |

## Today (2026-09-07)

| provider / endpoint / runner | implemented + offline-tested | live-smoke-tested | end-to-end migration verified |
| --- | :---: | :---: | :---: |
| OpenAI — `chat/completions` | yes | yes | **yes** — [shell_gpt, gpt-5.5 → gpt-5.6-sol](../reports/shellgpt-upgrade.md); [booking agent](../runs/real-56sol/REPORT.md) |
| OpenAI — `responses` | yes | yes | **yes** — the accepted endpoint-routing repair in both runs above |
| OpenAI — flex tier | yes | yes | yes (both runs above used it) |
| OpenAI — Message Batches | partial (`providers/openai_batch.py`, offline tests only) | no | no |
| Anthropic — `messages` | yes | yes | **yes** — [four Claude agents, Fable 5 → 5.1](../reports/fable-5-1-upgrade.md); [policybench](../reports/policybench-upgrade.md), [toponymy](../reports/toponymy-upgrade.md), [plastiq](../reports/plastiq-upgrade.md) |
| Anthropic — prompt caching | yes | yes | yes (cache hits recorded in the Fable 5.1 runs) |
| `upshift capture` (recording proxy) | yes | yes — [pydantic-ai smoke](../reports/capture-pydantic-ai-smoke.md), 3 cases at N=3 | no |
| `upshift capture` — the other 7 framework rows | yes (mapping verified from each framework's source) | no | no |
| `upshift adapt` — Python repos | yes | yes | yes — shell_gpt's adapter came out of `adapt` |
| `upshift adapt` — notebooks | yes | yes | no (adapters were reviewed by hand before the run) |
| `upshift adapt --from-capture` | yes | yes | no |
| Native runner (`agent.json` `runner` block, DESIGN §C) | pending — landing this sprint | no | no |
| `upshift verify-patch` (DESIGN §E) | pending — landing this sprint | no | no |
| Simulators (`sim-5.5`/`sim-5.6-*`, `sim-fable-5`/`5-1`) | yes | n/a | n/a — simulator results validate machinery and are never evidence |
| AWS Bedrock | **not implemented** | — | — |
| Google Vertex AI | **not implemented** | — | — |
| Google / Gemini direct | **not implemented** | — | — |
| Local models (Ollama, vLLM, …) | **not implemented** — an OpenAI-compatible base URL may work; untested | — | — |

## How to read a "no"

A **no** in the third column does not mean the path is broken. It means we have not run a
complete two-version migration on it and committed the record, so we will not describe a
result from it as verified. The rescue campaign's own classification worked the same way:
`REPAIRED_VERIFIED` required a committed run pair, and everything else got a different label.

## The scope label is the same distinction, per run

Every run carries a `scope` (DESIGN §A), derived from how it executed:

- `request_contract` — upshift built the requests from the adapter's three patchable files;
  the backend was a capture replay or a generated stub.
- `adapted_agent` — the adapter's `backend.py` executed real tool semantics.
- `native_application` — the application's own entry point ran, with its own
  request-building code.

Nothing in a report says "verified in the application" unless the scope is
`native_application`. Every migration in the third column above ran at `adapted_agent` scope
or narrower, which is exactly the gap the native runner closes.

## Where each claim is checked

- Offline coverage: `uv run pytest -q` (no network, no key, $0). The rescue regression suite
  is `tests/rescue/`.
- Live evidence: the committed run records under `runs/`, priced with `upshift cost` and
  diffed with `upshift diff`.
- Provider realness: `report.REAL_PROVIDERS` — a run from a provider outside that tuple is
  stamped `SIMULATED PROVIDER — machinery validation only` in its report, and
  `tests/test_report_provenance.py` pins the stamp in both directions.
