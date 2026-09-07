# ADAPTER.md — running upshift on your own agent

upshift evaluates **plain API agents**: one system prompt, a list of function tools, and a
tool-calling loop over OpenAI's `/v1/chat/completions` or `/v1/responses`, or Anthropic's
`/v1/messages`. No framework integrations (LangChain, CrewAI, Agents SDK, …), no other model
providers, no streaming, no multi-agent graphs (SCOPE.md). If your agent is not expressible
as the five files below, upshift cannot run it — that is a scope decision, not an oversight.

```
my_agent/
  agent.json          # {name, endpoint, model, params{}, system_prompt_file, tools_file, max_turns}
                      # optional: turn_params[] (per-turn param overrides), volatile_suffix,
                      #           terminal_tools[]
  system_prompt.txt   # the system message, verbatim
  tools.json          # OpenAI chat-style tools: [{"type": "function", "function": {...}}]
  backend.py          # create_backend(initial_state) -> object with .execute()/.state()
  cases/cases.json    # the eval suite
```

`upshift run --agent my_agent --run-id ... --model ...` and
`upshift upgrade --agent my_agent --baseline-model ... --candidate-model ... --tag ...`.

## The contract

1. **agent.json** requires `name`, `endpoint`, `model`, `system_prompt_file`, `tools_file`.
   `endpoint` is `"chat_completions"`, `"responses"` (OpenAI) or `"messages"` (Anthropic).
   `params` is passed to the API verbatim (`reasoning_effort` is mapped to `reasoning.effort`
   on `/v1/responses` and to `output_config.effort` on `/v1/messages`); `max_turns` caps
   assistant turns per episode (default 12). Declare sampling params (`temperature`,
   `top_p`, `top_k`) as plain `params` keys on every endpoint — how they TRAVEL is upshift's
   job: on `/v1/messages` they are moved into `extra_body` when the installed `anthropic` SDK
   no longer takes them as keywords (>= 1.1.0), which puts the same field on the wire and lets
   the API, not the client, decide. An `extra_body` you write yourself is honoured and wins,
   and the sampling repair can remove params from either place. `tools.json` is chat-style on every endpoint —
   upshift converts it for `responses` and `messages`.

   **`turn_params` (optional): params that change from turn to turn.** `params` is what every
   assistant turn sends. When your agent sends something different on different turns — the
   common one is a `tool_choice` that forces a tool on turn 1 and then goes `"auto"`, which is
   what every structured-output and routing agent does — declare the sequence:

   ```json
   "params": {"max_tokens": 512},
   "turn_params": [{"tool_choice": {"type": "any"}}, {"tool_choice": {"type": "auto"}}]
   ```

   Each entry is applied **over** `params` for that assistant-turn index (0-based), and the
   **last entry repeats** for every later turn, so a two-entry list reads "turn 1 like this,
   everything after it like that". A value of `null` **unsets** that param for that turn —
   "the field was not sent" is a different request from "the field was sent with a default",
   and a capture knows which happened. Absent or empty means every turn sends `params`, which
   is what nearly every hand-written agent wants. This is not cosmetic: under a forced
   `tool_choice` the model cannot answer in text, so an agent that forces on *every* turn
   calls tools until `max_turns` and fails its own `turns_at_most` **on the baseline model** —
   a suite that never worked, which the differ used to score as `SAFE`. The repair loop sees
   `turn_params`: removing a forced `tool_choice` removes it from the sequence too.
2. **backend.py** must expose `create_backend(initial_state: dict) -> Backend`, called once per
   episode with the case's `initial_state`.
   - `Backend.execute(name: str, arguments: dict) -> dict` runs one tool call and **never
     raises**: unknown tool name, missing argument, impossible operation are all returned as
     `{"error": "..."}`. Whatever it returns is JSON-encoded and fed back to the model verbatim,
     so error text is part of your agent's behavior.
   - `Backend.state() -> dict` returns a JSON-serializable snapshot of everything the tools
     changed. It is recorded in every rep file and is what `final_state` / `state_count` /
     `confirmation_id_valid` assert against.
3. **Determinism is a requirement, not a preference.** Every case runs N times (default 5)
   against both models and the differ compares pass counts across reps; a backend that reads
   the clock, the network, a random source or shared mutable state makes every case flaky and
   every number meaningless. Given the same `initial_state` and the same sequence of `execute`
   calls, a backend must produce the same results and the same final state. Keep it in memory.
4. **The repair loop may edit only three files**: `agent.json`, the system prompt file and the
   tools file — the four allowed repair types (prompt edit, model params, tool-schema edit,
   endpoint routing) are all expressible as edits to those. `backend.py` and `cases/cases.json`
   are never modified: they are the agent under test and the yardstick. The emitted patch is a
   `git apply`-able diff over exactly those three files.

## Case schema (`cases/cases.json`, a JSON array)

```json
{"id": "todo_add_one", "description": "Adds a single task the user fully specified.",
 "initial_state": {"tasks": []}, "user_messages": ["Add a task to call the dentist."],
 "checks": [{"type": "no_api_error"}, {"type": "tool_called", "name": "add_task"}],
 "sim": {"oracle_plan": [...]}}
```

`id` is a unique slug (it seeds the run deterministically). `user_messages` after the first are
sent in order, each once the agent finishes the previous turn; tool calls record which segment
they belong to. A case passes a rep iff **every** check passes. The `sim` block is read only by
the sim provider — never by checks.

## Check types

| check | semantics |
| --- | --- |
| `no_api_error` | The episode completed without an API error. An errored episode reports this single failed check and nothing else is evaluated. |
| `tool_called {name, min_times=1, max_times?, args_subset?, exact_args?, retrieval?}` | The named tool ran within the count bounds, and (if given) at least one call's arguments contained `args_subset` / equalled `exact_args`. `retrieval: true` marks the tool as a retrieval tool; it changes **nothing** about pass/fail and is read only by the differ, which uses it to report a dropped retrieval call as `reduced_retrieval_calls` instead of a generic missing call. |
| `tool_not_called {name}` | The named tool never ran. |
| `state_count {path, equals, where?}` | Entries at `path` in the final state (a list, or an object's values) matching every key/value in `where` number exactly `equals`. |
| `bookings_count {equals}` | Booking-flavored alias of `state_count {path: "bookings", where: {"status": "confirmed"}}`. Kept for the committed booking suite; write `state_count` instead. |
| `final_state {path, equals}` | Dot/bracket path into the final state (`tasks[0].status`) equals a value. |
| `no_tool_calls_after_success {name}` | Over-acting detector: within the final user segment, no tool call of any kind happens after `name` first succeeds. |
| `confirmation_id_valid {pattern?, state_path?, id_field?, known_from?}` | Id-fabrication detector: every identifier matching `pattern` in the final assistant message must be real. Defaults `pattern` `UPS-\d+`, `state_path` `bookings`, `id_field` `booking_id`, `known_from` `state`. Known ids are an object's keys plus each entry's `id_field`; `known_from` may be `state`, `tool_results` (ids some tool actually returned this episode) or `both`. Pass your own `pattern` — the default is the packaged example booking agent's format. |
| `response_contains {text}` / `response_not_contains {text}` | Case-insensitive substring of the final assistant message. |
| `response_matches {regex}` | `DOTALL`+`IGNORECASE` regex search on the final assistant message. |
| `turns_at_most {n}` | Efficiency contract: the episode used at most `n` assistant turns. Turns are counted as the number of **distinct `turn` values across the episode's tool executions** (every assistant turn that issued at least one tool call) **plus one** for the final assistant turn, which answers and calls nothing — so an episode with no tool calls is 1 turn. In a multi-segment case an intermediate answer turn is counted only when it also called a tool. Wall time is never asserted. |

An unknown check type or a malformed check is reported as a failed check with a reason; it
never crashes a run. Checks are deterministic by design — there is no LLM judge in v1.

## Failure signatures the repair loop understands

The differ classifies each failing case into signatures that drive candidate generation.
Besides the OpenAI-era ones, it recognizes the documented Claude Fable 5 → 5.1 changes
(DESIGN.md): `api_error_forced_tool_choice` (the 400 for `tool_choice` type `tool`/`any`) →
drop the param and state the requirement in the prompt; `api_error_unsupported_sampling_params`
(a 400 naming `temperature`/`top_p`/`top_k`) → drop those params, from `params` or from
`params.extra_body`; `serialized_tool_calls` (the
candidate stopped batching tool calls the baseline batched, or blew a `turns_at_most` budget the
baseline met) → append the documented batching instruction, and raise effort; and
`reduced_retrieval_calls` (a `tool_called` check fails for a retrieval-marked or
retrieval-named tool that the baseline actually called) → raise reasoning effort one rung on
the endpoint's ladder (`messages`: low<medium<high<xhigh<max, unset = high; `chat_completions`
/ `responses`: none<low<medium<high, unset = medium), then append the documented verification
nudge. Effort is only ever raised, never lowered. The two comparative signatures need the
baseline run's reps for the same case; without them they simply do not fire.

The one signature the loop **refuses**: `thinking_block_invalid` (the 400 ``Invalid `signature`
in `thinking` block``). No edit to the three patchable files can fix it — the fix is runtime
history handling (strip the invalidated run's thinking blocks, or set
`thinking.block_binding.prefix_mismatch_behavior: "drop_block"` under the
`thinking-binding-controls-2026-08-01` beta) — so the loop logs a REFUSAL line with that
pointer and generates no candidate rather than spending budget on repairs that cannot work.

## What the sim provider can and cannot do for a foreign agent

`--provider sim` (models `sim-5.5` / `sim-5.6-sol`) exists to exercise the machinery with no
API key and no cost. It **replays a per-case script**, so every case must carry
`sim.oracle_plan`; without one the run stops with a single error telling you to use
`--provider openai`. Plan format and reference syntax are documented at the top of
`src/upshift/providers/sim.py`; `tests/todo_agent/cases/cases.json` is a worked non-booking
example, and `tests/test_foreign_agent.py` drives that agent through the whole pipeline.

Works for any agent: the documented `gpt-5.6` hard 400 on chat/completions + tools, the
`over_acting` corruption, `flaky` cases, the differ, the repair loop, the patch, the verdict.
Needs one declaration: `duplicate_call` and `skip_tool` target `sim.critical_tool` (default
`book_flight`) and `skip_tool` invents an id starting with `sim.fabricated_id_prefix` (default
`UPS-9`) — set both, and make the prefix match your `confirmation_id_valid` pattern, or those
two corruptions silently never fire.

Honesty rule (DESIGN.md): sim runs validate the machinery, never the thesis. The sim's response
to a repair is true by construction. Only `provider=openai` runs are evidence about a model.

## Victim-flavored but optional

Everything below has a booking-agent default and is a no-op or an opt-in for anyone else:
`bookings_count` (use `state_count`); the `confirmation_id_valid` defaults; the sim's
`critical_tool` / `fabricated_id_prefix` defaults; and the two tool-schema repair candidates,
which target a tool literally named `book_flight` and are skipped for an agent that has no such
tool. Every other repair candidate — the prompt blocks, `reasoning_effort`, endpoint routing —
is domain-neutral and is appended or applied verbatim to whatever agent is under repair.

## Addendum: agent directories built by `upshift adapt --from-capture` (v0.4)

A capture-derived directory satisfies the contract above exactly — runner, differ, repair loop,
patch and verdict do not know where it came from. Four optional `agent.json` keys are what it
adds, and every one of them is read out of the recorded bytes, never inferred:

- **`turn_params`** (list of param objects) — the contract above, derived. A param is written
  here, and left out of `params` entirely, exactly when one recorded conversation sent
  different values for it on different turns. Where the recorded conversations disagree with
  each other in a way no sequence can express, `adapt` does **not** pick a winner quietly: it
  lists every variant with its count in `ADAPT_EDITS.md` under CONFLICTS, and
  `adapt --from-capture --strict` exits non-zero. A tie is broken by canonical text and
  reported — never by which request the recorder happened to see first.

- **`volatile_suffix`** (string) — text the agent regenerates on every request and hangs off
  the trailing user turn (a live facts block: `current_time`, a session id). ONE recorded
  sample, appended by `agent_loop` at request-building time, never stored in the conversation
  history and never regenerated, because a value that changed per rep would make every case
  flaky. On `messages` it lands as a trailing text block on the last user message, after any
  `tool_result` blocks.
- **`terminal_tools`** (list of names) — tools whose call ENDS the episode, because the
  capture shows the framework never answered one: a `tool_use` id that no later request
  returns a `tool_result` for. pydantic-ai's `final_result` (a structured `output_type`) is
  the canonical case. upshift records the call, does not ask the backend for a result the
  framework never had, does not invent one, and stops — with the call's arguments as the
  episode's final message, because that is the framework's own output. A tool answered even
  once anywhere in the capture is never terminal.
- **`capture`** (object) — provenance: the capture directory, the detected framework, and the
  request/conversation counts. The framework name is what makes the report's "Framework
  mapping" section possible (`docs/framework-mapping.md`); a hand-written agent directory has
  no framework and gets no such section.

`turn_params`, `volatile_suffix` and `terminal_tools` are all legal in a hand-written
`agent.json` too. All default to absent, which is what nearly every agent wants.

The generated `backend.py` is a **replay, not a re-implementation**: it looks up (tool name,
canonical arguments) in `recorded_tools.json` and returns the result the real tool really
returned. Arguments that were never recorded return
`{"error": "no recorded result for these arguments"}` — deliberately, because a repaired
candidate that calls a tool with new arguments has left the ground the capture covers, and an
invented answer would turn "we do not know" into a passing case. This is the one place where a
capture-derived agent is weaker than a hand-written one: the further a repair moves the model
off the recorded path, the more of the suite goes unanswered. `ATTRIBUTION.md` and
`ADAPT_EDITS.md` in the generated directory name every source and every deviation.

## Native runner — when your application should run itself (v0.5)

Everything above assumes the agent can be expressed as five files. Sometimes it cannot, and
sometimes it should not be. The failing request is built inside a framework; the tools are a
CAD kernel or a benchmark harness; the project is TypeScript and `upshift adapt` writes Python.
In the two migration-rescue tracks that is not a corner: 25 OpenAI-track cases and 36
Anthropic-track cases closed `UNSUPPORTED_FRAMEWORK`, and four of the five hand-built adapters
were hand-built because the target was not Python (rescue-ops
`ops/anthropic/summaries/EXEC_SUMMARY.md` §(f)). Two of those projects — `auditk/auditk`
(`A-052`) and `PolicyEngine/policybench` (`A-032`) — *are* benchmark harnesses: they already
run one scenario end to end with the application's own request-building code, and the adapter
was a reconstruction of something that already ran.

A **native runner** drives that command instead. Add a `runner` block to `agent.json` and
upshift stops building requests entirely:

```json
{
  "name": "my-app",
  "model": "claude-fable-5-1",
  "runner": {
    "kind": "command",
    "workdir": ".",
    "command": ["python", "-m", "myapp.eval_case"],
    "env": {"MYAPP_MODEL": "{model}", "MYAPP_ENDPOINT": "{endpoint}"},
    "timeout_s": 120,
    "max_output_bytes": 1048576,
    "isolation": "workdir-copy"
  }
}
```

With a `runner` block, `system_prompt_file`, `tools_file` and `backend.py` are **not required
and not used** — `runner` + `cases/cases.json` is the whole authoring surface. `workdir` is
relative to the agent directory and may not escape it; `command` is an **argv list**, never a
shell string (there is no shell anywhere in this path); `isolation` is `workdir-copy` (default:
a fresh temp copy per rep, deleted afterwards) or `in-place` (needs `--allow-in-place`).
`{model}`, `{endpoint}`, `{case_id}`, `{rep}` and `{seed}` are the only template variables
`env` accepts.

### upshift result protocol v1

upshift runs the command once per (case, rep) and writes one JSON object to its **stdin**:

```json
{"protocol": 1, "case_id": "...", "rep": 1, "seed": 12345, "model": "...", "endpoint": "...",
 "initial_state": {}, "user_messages": ["..."], "patch_applied": false}
```

The command prints **one JSON object as the last line of stdout** (everything else goes to
stderr — your test runner's own output is fine):

```json
{"protocol": 1,
 "final_message": "...",
 "tool_executions": [{"turn": 0, "segment": 0, "name": "...", "arguments": {}, "result": {}}],
 "final_state": {},
 "api_calls": [{"request": {}, "response": {}, "error": null}],
 "api_error": null,
 "usage": {"input_tokens": 0, "output_tokens": 0}}
```

`api_calls` and `usage` are optional; everything else has a default. Checks are evaluated on
that result **by the same engine that evaluates an adapter episode** — same check types, same
N reps, same thresholds, same statistics, same verdict. There is no second verification engine.

Two reference implementations ship in `examples/runners/`: `python_runner.py` and
`node_runner.mjs`, each driving a small real agent loop against a stub provider, with tests.

**Report a provider error as `api_error`, not as a non-zero exit.** A 400 is the most valuable
thing a run can observe — it is the whole subject of the gpt-5.6 and Fable 5.1 migrations.
Exiting non-zero tells upshift that *its own harness* failed, and it will correctly refuse to
treat that as evidence about a model.

### The failure class that is not a regression

A non-zero exit, a timeout, malformed JSON, a missing `protocol` key, or output past
`max_output_bytes` is recorded as an `api_error` with `error_type: "runner_error"` — a
**non-behavioural** failure. It is never scored as a model regression; it makes the run
inconclusive for that case. The same class covers `continuation_exhausted` (below). A flaky
test command therefore cannot manufacture a regression.

### Authorization — upshift will not execute your repository by accident

Running a native agent means running code from the application's checkout, once per case per
rep. It requires `--allow-runner` (or `UPSHIFT_ALLOW_RUNNER=1` for CI). Without it upshift
prints the exact argv, working directory, isolation mode and the environment variables it
would set, executes nothing, and exits 2.

The child gets a **minimal environment**: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`,
`SYSTEMROOT` where present, the `runner.env` you declared, and the API-key variables of the
run's provider only — never the rest of your environment. A timeout kills the whole process
group, so a command that spawns workers does not leave them running (or spending). `--capture`
additionally starts the `upshift capture` recorder on **loopback only** and points the child's
provider base URL at it, attaching the requests the application itself serialized to each rep
record as `wire_requests`; it forces `--workers 1`, because a request can only be attributed to
a rep that ran alone.

### The boundary: native mode detects and verifies, it does not repair

The repair loop edits `agent.json`, the system prompt and the tool schemas. A native agent has
none of those under upshift's control — the request is built by the application's own source,
which upshift does not edit. **No repair candidate is generated in native mode.** What native
mode does is detect the regression against the real application and verify a patch *you*
supply (`upshift verify-patch`), and the report says which of the two it did. Only a native run
carries `scope: native_application`, and no wording anywhere may claim a change was "verified
in the application" at any other scope.

## Addendum: continuation and fidelity for capture-derived directories (v0.5)

Three more `agent.json` keys, all written by `upshift adapt --from-capture` and all legal by
hand:

- **`recorded_turns`** (integer) — how many assistant turns the deepest recorded conversation
  contained. It is what "the end of the recording" means.
- **`continuation`** — `"fail"` (default) or `"repeat_last"`. When a replayed episode needs a
  turn past `recorded_turns`, there are no recorded parameters for it, because the framework
  never sent any. `fail` ends the episode with a `continuation_exhausted` error — the same
  non-behavioural class as `runner_error`, so the case is inconclusive rather than regressed.
  `repeat_last` reuses the last recorded turn's parameters and writes `continuation_used: true`
  into every rep record that did so. Never silently, in either direction. An agent without
  `recorded_turns` (every hand-written one) is unaffected: there is no recording to run off.
- **`unsupported_fields.json`** (a file, not a key) — every field the recording held that an
  agent directory has no slot for: dropped request params (`response_format`, `metadata`,
  `mcp_servers`), tool fields the chat-style shape cannot carry (a server tool's `type` and its
  own configuration), beta headers, response blocks with no representation, non-2xx responses
  in the capture, a non-provider upstream (a gateway's contract is not the provider's), and the
  API paths the recorder does not record at all (`count_tokens`, `batches`). Each finding has a
  `field`, `where`, `kind`, `count`, `detail` and `sample`; the same rows are rendered as a
  table in `ADAPT_EDITS.md`. The list is denylist-free: anything upshift does not carry is
  reported, so a field a provider adds tomorrow shows up as a finding on its first capture
  rather than as silence. This list is the boundary of what a run on that directory can
  measure — a regression that depends on a field listed there cannot be detected, and a `SAFE`
  verdict says nothing about it.
