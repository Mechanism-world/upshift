# ADAPTER.md — running upshift on your own agent

upshift evaluates **plain API agents**: one system prompt, a list of function tools, and a
tool-calling loop over OpenAI's `/v1/chat/completions` or `/v1/responses`, or Anthropic's
`/v1/messages`. No framework integrations (LangChain, CrewAI, Agents SDK, …), no other model
providers, no streaming, no multi-agent graphs (SCOPE.md). If your agent is not expressible
as the five files below, upshift cannot run it — that is a scope decision, not an oversight.

```
my_agent/
  agent.json          # {name, endpoint, model, params{}, system_prompt_file, tools_file, max_turns,
                      #  volatile_suffix?}
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
   assistant turns per episode (default 12). `tools.json` is chat-style on every endpoint —
   upshift converts it for `responses` and `messages`. The optional `volatile_suffix` is a
   fixed string appended to every request after the whole conversation (see "Volatile
   suffix" below).
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
   The same rule covers `volatile_suffix`: it is a literal, sent verbatim, never templated.
4. **The repair loop may edit only three files**: `agent.json`, the system prompt file and the
   tools file — the four allowed repair types (prompt edit, model params, tool-schema edit,
   endpoint routing) are all expressible as edits to those. `backend.py` and `cases/cases.json`
   are never modified: they are the agent under test and the yardstick. The emitted patch is a
   `git apply`-able diff over exactly those three files.

## Volatile suffix (`agent.json` → `volatile_suffix`, optional)

Some harnesses append a message to **every** outgoing request, after the conversation, rather
than as a user turn: a live-facts block (current time, session facts), a per-request reminder.
Anthropic's prompt-caching guidance makes the pattern common — volatile content goes *behind*
the cached prefix. A case script cannot express it: `user_messages` are turns the model answers
one at a time, while this block rides along on every call and is never answered.

```json
{"name": "...", "endpoint": "messages", "model": "claude-fable-5", "params": {},
 "system_prompt_file": "system_prompt.txt", "tools_file": "tools.json",
 "volatile_suffix": "<dynamic_facts>\ncurrent_time: 2026-09-02T12:00:00Z\n</dynamic_facts>"}
```

What upshift does with it:

- On **every** request in an episode — the first call, every tool-result turn, every later
  segment — it appends one `{"role": "user", "content": <volatile_suffix>}` as the **last**
  item of `messages` / `input`, on all three endpoints. Nothing comes after it. The recorded
  request in each rep file shows it exactly where the model saw it.
- It is appended to the **outgoing request only**, never to the conversation history the loop
  replays, so a five-turn episode sends it five times and the model sees it once per request.
- On `messages` it sits **after both cache breakpoints** (system block, last tool definition)
  and never carries one itself, so the cached prefix stays cached and the suffix is the volatile
  tail Anthropic's guidance describes. After a tool turn it follows the `tool_result` user
  message; Anthropic combines consecutive user turns, `tool_result` blocks first, which is the
  order the API requires. The OpenAI endpoints get the same placement, which is also what their
  automatic prefix caching wants.
- **It is a literal.** upshift never templates, formats or evaluates it (rule 3). If upstream
  fills the block with live values, freeze them to the same instant your `backend.py` freezes
  its clock, and record the frozen values in your adapter notes. Absent or `null` means "not
  sent"; a non-string or an empty string is an authoring error caught before any model call.
- It lives in `agent.json`, so it is part of the hashed, recorded, patchable surface — but no
  repair candidate edits it. The candidate that does the same job is a system-prompt edit.

Where it comes from: case A-015 of the Anthropic rescue lab (everruns). That engine appends a
dynamic-facts block, with the current time, as a trailing user message on every request; its
model is told the time and then asked what time it is. Without the block the adapter's model has
no clock, must call the time tool, and the suite cannot see the regression upstream reported.

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
(a 400 naming `temperature`/`top_p`/`top_k`) → drop those params; `serialized_tool_calls` (the
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
