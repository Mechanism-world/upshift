# ADAPT_EDITS — human edits on top of the `upshift adapt` output

Baseline: `upshift adapt <FACT clone>` output (ADAPT_REPORT.md in that directory; extraction
model `gpt-5.5` via `openai-flex`, 2 rounds, $0.2474). Every file below was copied from that
output first and then edited; this is the ledger of the edits.

## Counts

| file | edits | verdict |
| --- | --- | --- |
| `agent.json` | 2 | usable as generated, 2 one-line changes |
| `system_prompt.txt` | 1 | correct except one byte |
| `tools.json` | **0** | shipped exactly as generated |
| `backend.py` | rewritten (2 of 3 tools were TODO stubs) | not usable as generated |
| `cases/cases.json` | rewritten (2 drafted cases dropped, 7 written) | not usable as generated |
| `fact_demo.db` | new file (adapt does not copy fixtures) | — |
| `ATTRIBUTION.md`, `ADAPT_EDITS.md` | new files | — |
| **total discrete edits to generated files** | **3** + 2 rewrites | |

## `agent.json` — 2 edits

1. `model`: `"claude-3-haiku-20240307"` → `"claude-fable-5"`. adapt extracted the *source*
   default correctly (`src/core/config.py:168`); the migration baseline is a deployment choice
   (ATTRIBUTION, "The model").
2. `params.timeout`: `30` removed. Correctly extracted from `driver.py:521`, but it is an SDK
   transport setting, not part of the request body — upshift's provider owns transport.

Kept as generated: `endpoint: "messages"`, `params.max_tokens: 4096`,
`params.tool_choice: {"type": "any"}` (the break under test), `max_turns: 6`,
`system_prompt_file`, `tools_file`, `name`.

## `system_prompt.txt` — 1 edit

adapt wrote 878 bytes; the upstream literal is 877. It had appended a trailing newline. Removed,
so the file is now byte-identical to `src/core/config.py:141-162`. Verified mechanically by
re-extracting the triple-quoted literal from `config.py` and comparing.

## `tools.json` — 0 edits

Checked against `ToolRegistry._extract_schema` (`src/tools/decorators.py:250-270`) applied to
the three `@Tool` declarations: names, descriptions, `properties` (including `minLength`/
`maxLength` on `statement`) and the derived `required` lists all match. adapt's ADAPT_REPORT
flagged these three lines as "templated — must review"; review found nothing to change.

## `backend.py` — rewritten (332 lines vs. the generated 157)

adapt generated a generic dispatch scaffold with `TOOL_SPECS`, of which:

- `SQL_QueryReadonly` → `{"kind": "unclear"}`, i.e. every call returned
  `{"error": "TODO(adapt): not implemented — …"}`;
- `SQL_GetSchema` → same;
- `SQL_GetSampleQueries` → `{"kind": "list", "state_key": "sample_queries"}`, which returns
  `{"results": []}` from `initial_state` — the right *shape family*, the wrong tool: upstream's
  list is a hardcoded constant, not state, and the envelope is
  `{sample_queries, total_queries, status, execution_time_ms}`.

Its report was honest about this ("Database contents and exact query results … are not included
in the evidence", `backend.py` confidence **low**). Nothing was salvageable except the
`create_backend` / `execute` / `state` contract skeleton and the never-raises `try/except`,
which the rewrite keeps. What replaced it:

- real SQLite execution against a committed copy of `data/fact_demo.db`, opened `mode=ro`;
- `validate_sql_query` and `_is_valid_table_name` ported from `src/db/connection.py:321-472`;
- upstream's success and failure result envelopes from `src/tools/connectors/sql.py`;
- `SQL_GetSchema` ported from `sql.py:192-273`;
- `SAMPLE_QUERIES` copied verbatim from `sql.py:291-323`;
- clock-derived fields (`query_id`, `execution_time_ms`) replaced by deterministic stand-ins;
- a real `state()`: `queries_run`, `statements`, `rejected_statements`, `schema_calls`,
  `sample_query_calls`, `last_row_count`, `last_columns`, `last_rows`.

Root cause, for the adapt roadmap: the two stubbed tools are stubbed because their semantics
live in a **data file** (`data/fact_demo.db`) and a **second module** (`src/db/connection.py`)
that the ranked evidence bundle never surfaced. adapt's round-2 pointer following did reach
`sql.py` and `config.py`, but a binary fixture is outside what the extractor can read at all.
"Copy the committed fixture the config points at, and follow the delegate call
(`self.database_manager.execute_query`) into its module" is the concrete v0.3 ask this target
produces.

## `cases/cases.json` — rewritten (7 cases; both generated cases dropped)

adapt drafted 2 cases, both from documentation prose rather than the agent's behaviour:

- `simple-arithmetic-driver-smoke` — asserts the response contains `"2 + 2 = 4."`, taken from
  a debugging write-up (`docs/anthropic_api_debugging_resolution.md:128`). It tests neither the
  tools nor the database, and with `tool_choice: any` in force the agent cannot answer in text
  at all.
- `query-processing-cache-read` — asserts the response contains `"Test response"`, which is a
  **mock return value** from `docs/testing-strategy.md:101`, not anything the agent would ever
  say.

Both were dropped. The seven replacements and their grounding are listed in ATTRIBUTION.md;
every asserted value was computed from the shipped database through this backend, and none of
them appears in the prompt, the tools or the question.

## New files

- `fact_demo.db` — `data/fact_demo.db` copied byte-for-byte (81,920 bytes, md5
  `a0ff57cf5c8474083321514146bbade9`); no reduction needed under the 2 MB ceiling.
- `ATTRIBUTION.md`, `ADAPT_EDITS.md`.

## Verification after the edits

`upshift`'s own machinery, no API calls: `validate_agent_dir` passes; `run_suite` on
`sim-fable-5` passes 7/7 cases; `sim-fable-5-1` fails 7/7 with the documented
`tool_choice: type "tool" and "any" are not supported for this model.` 400. Covered by
`tests/test_agents_claude_a.py`.

A full `upshift upgrade --provider sim` (N=5, run records written outside the repo) ends
SAFE WITH PATCH: 7/7 regressed restored, 0 broken, in **two** accepted candidates —
`remove-forced-tool-choice` restores 3 (the schema, sample-query and refusal cases) and
`raise-effort-one-rung` (to `xhigh`) restores the other 4. The second step happens because
`SQL_QueryReadonly` matches the sim's retrieval-name heuristic, so its `skip_retrieval`
corruption fires below `xhigh` — this target therefore exercises two of the four documented
5 -> 5.1 detectors, not one. Sim results validate the machinery, never the thesis.
