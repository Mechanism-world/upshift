# ATTRIBUTION — FACT as an upshift agent directory

## Upstream

- Repository: <https://github.com/ruvnet/FACT> (FACT — "Fast-Access Cached Tools")
- Commit extracted from: `b0e343583cd8f64549dbcba4b7c709e48b3a6a08`
  ("fix: security vulnerabilities + dep updates (#4)", 2026-05-23)
- License: MIT, Copyright (c) 2025 rUv. The prompt text, tool schemas, sample-query list and
  demo database reproduced here come from that MIT-licensed source; the full license travels
  with the upstream repo.

FACT's query driver is a plain Anthropic Messages agent — one system prompt, three tools, a
tool-calling loop — which is exactly upshift's `messages` endpoint scope, so it needs no
framework shim.

**Why this target.** `src/core/driver.py:523` sends `tool_choice={"type": "any"}` on *every*
call whenever tools are present. That is one of the documented Fable 5 → 5.1 breaks
(DESIGN.md, `api_error_forced_tool_choice`): 5.1 returns
`tool_choice: type "tool" and "any" are not supported for this model.` The value is hardcoded
at the call site — no env var, no config knob — so a FACT user has no way to configure around
it. Keeping it is the point of the target.

## What was extracted, and from where

| Artifact | Upstream source |
| --- | --- |
| `system_prompt.txt` | `src/core/config.py:141-162` — the default of the `Config.system_prompt` property (`os.getenv("SYSTEM_PROMPT", """...""")`), byte-identical (877 bytes, no trailing newline). |
| `tools.json` | `src/tools/connectors/sql.py` — the three `@Tool(...)` declarations at lines 135 (`SQL_QueryReadonly`), 185 (`SQL_GetSchema`) and 276 (`SQL_GetSampleQueries`), as `ToolRegistry._extract_schema` (`src/tools/decorators.py:250-270`) renders them: `{name, description, input_schema:{type:"object", properties:<the decorator's `parameters` dict verbatim>, required:<params with no default>}}`. |
| `agent.json` | `src/core/driver.py:516-524` — `client.messages.create(model=…, system=…, messages=…, max_tokens=4096, timeout=…, tools=…, tool_choice={"type":"any"} if tools else None)`. `max_turns: 6` from the driver's one initial tool round plus `max_iterations = 5` (`src/core/driver.py:248`). |
| `fact_demo.db` | `data/fact_demo.db`, copied byte-for-byte (81,920 bytes, md5 `a0ff57cf5c8474083321514146bbade9`). It is the default `DATABASE_PATH` (`src/core/config.py:121`). Well under the 2 MB ceiling, so no reduced fixture was needed. |
| `backend.py` | `src/tools/connectors/sql.py` (tool bodies) and `src/db/connection.py:321-551` (`validate_sql_query`, `_is_valid_table_name`, `execute_query`). |

### Tool-schema shape

`tools.json` is chat-style (`{"type":"function","function":{name,description,parameters}}`)
because that is the ADAPTER.md contract for every endpoint; `agent_loop` converts `parameters`
→ `input_schema` before the request goes out, which reproduces upstream's Anthropic shape
exactly. Verified by comparing the emitted request against `_extract_schema`'s output.

## The model

`agent.json` sets `claude-fable-5` — the baseline of the migration under test.

**This is not FACT's default.** `Config.claude_model` (`src/core/config.py:168`) returns
`os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307")`, so a stock checkout runs
`claude-3-haiku-20240307`. That default is itself stale relative to the repo's own shipping
config: `.env.template:66` sets `CLAUDE_MODEL=claude-3-5-sonnet-20241022`, so the model a FACT
deployment actually uses is an environment choice, not a source constant. `claude-fable-5` is
the deployment whose upgrade we are testing. No upstream file claims FACT runs on Fable 5, and
this session had no network access to check what any live deployment sets.

## Params: what was kept and what was dropped

| upstream | here | why |
| --- | --- | --- |
| `max_tokens=4096` | kept | required by the API; the driver's literal. |
| `tool_choice={"type":"any"}` | **kept** | this is the break under test. |
| `timeout=30` (`REQUEST_TIMEOUT`) | dropped | an SDK transport setting, not part of the request body; upshift's provider owns transport. Behaviourally inert. |
| `system=`, `tools=`, `messages=` | kept | carried by the five-file contract itself. |

## Backend: reimplemented against the real database

`backend.py` runs the model's SQL against the committed `fact_demo.db`, opened read-only
(`sqlite3.connect("file:…?mode=ro", uri=True)`). Mirrored faithfully:

- **Result shape.** `SQL_QueryReadonly` returns upstream's
  `{query_id, rows, row_count, columns, execution_time_ms, statement, status}`
  (`sql.py:73-83`), including the `statement[:100] + "..."` echo, with rows as dicts built from
  `sqlite3.Row` exactly as `DatabaseManager.execute_query` does (`connection.py:503-517`).
  Failures come back as upstream's failure envelope
  `{query_id, error, error_type, execution_time_ms, statement, status:"failed"}` (`sql.py:104-115`).
- **Validation.** `validate_sql_query` reproduces `connection.py:321-431` clause for clause:
  SELECT-or-`PRAGMA table_info` only, the same 16 dangerous keywords, the same nine injection
  patterns, the 5000-character cap and the 5-subquery cap, with upstream's exact error strings.
- **Schema.** `SQL_GetSchema` reproduces `sql.py:192-273`: `sqlite_master` ordered by name,
  `_is_valid_table_name` filtering, `PRAGMA table_info` per table, and the same
  `{name, type, nullable, primary_key}` column records, returning
  `{tables, total_tables, database_type:"SQLite", status}`.
- **Sample queries.** `SAMPLE_QUERIES` is the seven-entry list at `sql.py:291-323`, copied
  verbatim (only re-wrapped across source lines); the response keeps upstream's
  `{sample_queries, total_queries, status, execution_time_ms: 0}`.

### Deltas from the real runtime

- **Determinism substitutions.** Upstream stamps `query_id = f"query_{int(time.time()*1000)}"`
  (`sql.py:65`) and measures `execution_time_ms` with a wall clock. Both read the clock, which
  ADAPTER.md requirement 3 forbids: `query_id` is a per-episode counter (`query_1`, `query_2`,
  …) and `execution_time_ms` is always `0.0`. Nothing else about the results changes.
- **Sync, not async.** Upstream's tools are `async` behind a `DatabaseManager` connection pool.
  The backend is synchronous `sqlite3` with a fresh connection per call. Same SQL, same rows.
- **No validation cache.** `validate_sql_query`'s md5 cache (`connection.py:333-341`) only
  skips repeat work; dropped.
- **Syntax validation.** Upstream runs `EXPLAIN QUERY PLAN` on a separate writable connection
  before executing. Here a syntax error surfaces from the execute itself and is returned as the
  same `status: "failed"` envelope (`error_type: "DatabaseError"`, upstream's
  `Query execution failed: …` wording) rather than `InvalidSQLError`. Same refusal, different
  `error_type` label on malformed SQL.
- **Read-only handle.** Upstream opens the database read-write and relies on
  `validate_sql_query` alone. Here `mode=ro` means a write cannot succeed even if validation
  were bypassed — strictly stronger, and it makes the read-only contract testable.
- **No cache layer.** FACT's whole selling point is its prompt/tool cache (`cache_control`,
  `_call_llm_with_cache`, `src/core/driver.py:505-512`). upshift measures behaviour per
  episode, so every rep is an uncached call. This changes cost and latency, never the request
  body's prompt/tools/params.
- **No `state` upstream.** FACT's tools have no session state. `Backend.state()` is upshift's
  own deterministic summary of what the tools did — `queries_run`, `statements` (accepted),
  `rejected_statements`, `schema_calls`, `sample_query_calls`, `last_row_count`,
  `last_columns`, `last_rows` — so `final_state` / `state_count` checks have something real to
  assert against.
- **Turn cap.** `max_turns: 6` mirrors the driver's 1 + 5 iteration structure, but upstream's
  loop breaks as soon as a response has no `tool_use` block, whereas here `tool_choice: any`
  forces a tool call on every turn, so an unpatched episode can run the cap out.

## Eval cases

Seven cases in `cases/cases.json`; FACT ships no eval suite, so these are upshift's. Grounded
in the repo's own README usage examples and in the tools themselves:

| case | grounding |
| --- | --- |
| `tech_sector_companies` | README.md:433, verbatim question. |
| `tech_sector_employee_total` | aggregate over the same sector. |
| `q1_2025_second_highest_revenue` | README.md:449 ("Compare Q1 2025 revenue growth across all sectors"), narrowed to one deterministic answer. |
| `healthcare_vs_energy_q1_profit` | the README's cross-sector comparison shape. |
| `sample_queries_offered` | `SQL_GetSampleQueries`' own hardcoded list. |
| `schema_tables_listed` | `SQL_GetSchema`. |
| `readonly_rejects_write` | the read-only contract the tool description advertises. |

Every asserted value was computed from `fact_demo.db` through this backend, and every one is
**absent from the system prompt, the tool schemas and the question** — the anti-leak discipline
the shell_gpt suite uses. `TechCorp` is the one company the prompt names, which is why the
revenue case asks for the *second*-highest and the sector case asserts the other four names.

### Facts we could not verify

- **The README's own numbers are not this database's.** README.md:435-441 prints "Total: 15
  technology companies" and market caps ($489B for TechCorp) that do not exist in
  `data/fact_demo.db`, which holds 9 companies, 5 of them Technology, TechCorp at $250B. The
  README output is illustrative. Every check here uses values read from the shipped file, never
  from the README.
- **Star count.** The "183 stars" figure comes from the task brief; this session had no network
  access to confirm it.
- **Runtime environment.** `CLAUDE_MODEL`, `SYSTEM_PROMPT`, `DATABASE_PATH` and
  `REQUEST_TIMEOUT` are all environment-overridable. What a given FACT deployment actually
  sends is unknowable from the source; this directory encodes the source defaults plus the
  model choice stated above.
- **The `README.md:488` example** ("What are the quarterly revenue trends for technology
  companies?") is not a case, because `financial_records` covers only companies 1–5 while
  `financial_data` covers all 9 with the same columns, so "the" trend for technology companies
  has two defensible answers. Asserting either would be asserting our own SQL, not the agent's
  behaviour.
- **No "no tools needed" case.** The brief asked for one if the prompt allowed it. It does not:
  the system prompt says "You MUST use tools", and `tool_choice: any` makes a text-only reply
  impossible at the API level. A case asserting a tool-free answer would be untestable against
  this agent, so none was written.

## Reproducing

```shell
uv run pytest -q tests/test_agents_claude_a.py
upshift upgrade --agent agents/fact --provider sim \
    --baseline-model sim-fable-5 --candidate-model sim-fable-5-1 --tag fact-sim
```

Sim results validate the machinery, never the thesis (DESIGN.md).
