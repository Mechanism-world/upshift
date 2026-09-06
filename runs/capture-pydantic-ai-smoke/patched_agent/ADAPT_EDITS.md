# ADAPT_EDITS — every deviation from the recorded bytes

`upshift adapt --from-capture` copies the wire. Where it could not copy exactly, the deviation is listed here. Nothing else was changed.

## Structural deviations (always true of a capture-derived adapter)

1. **Thinking blocks are not replayed.** `thinking` / `redacted_thinking` blocks in the captured history are dropped from the cases. A thinking block's signature is valid only against the exact turn that produced it, so replaying one from another model's run is what produces the ``Invalid `signature` in `thinking` block`` 400 in the first place. The differ keeps its detect-and-refuse for that signature (ADAPTER.md); capture mode does not try to route around it.
2. **Tool schemas are re-wrapped.** Recorded `{name, description, input_schema}` is written chat-style, because ADAPTER.md requires that shape on every endpoint; `agent_loop.convert_tools_messages` converts it back before the request goes out, so the wire body is the recorded one.
3. **`cache_control` marks are dropped** from the system and tool definitions. upshift places its own cache breakpoints (`agent_loop._mark_last_tool_cacheable`), so keeping the framework's would double them.
4. **Checks are derived, not invented.** Only `no_api_error`, `tool_called` for tools the recording shows being called, and `turns_at_most` (recorded assistant turns + 1). No check is built out of the recorded answer text: that would be an assertion written from the model's own output.
5. **`sim.oracle_plan` is the recorded behaviour**, replayed by `--provider sim` only. It is never read by a check (ADAPTER.md), and a sim run is machinery validation, never evidence.
6. **The backend is a replay, not a re-implementation.** Unknown arguments return `{"error": "no recorded result for these arguments"}` rather than a plausible answer.

## Deviations specific to this capture

- tool(s) ['final_result'] were called but never answered with a tool_result anywhere in the capture, so the framework ended the conversation on them; written as agent.json `terminal_tools` and the episode stops there too
- temperature_c: a recorded tool result was a int, wrapped as {"content": ...} because ADAPTER.md requires execute() to return a dict
- temperature_c: a recorded tool result was a int, wrapped as {"content": ...} because ADAPTER.md requires execute() to return a dict
- temperature_c: a recorded tool result was a int, wrapped as {"content": ...} because ADAPTER.md requires execute() to return a dict

## Not deviations, but read before trusting a run

- `max_turns` is 3 — the longest recorded conversation plus one, so a repaired candidate has room for one extra turn without being cut off.
- Every case starts from an empty `initial_state`: a capture records answers, not a world to seed.
- Params seen across the capture: `{"max_tokens": [4096], "stream": [false], "tool_choice": [{"type": "any"}]}`.
