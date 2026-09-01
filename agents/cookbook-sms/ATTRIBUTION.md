# ATTRIBUTION — the cookbook SMS chatbot as an upshift agent directory

## Upstream

- Repository: <https://github.com/anthropics/claude-cookbooks>
- File: `tool_use/tool_choice.ipynb`, the **"Any"** section (the last section of the notebook)
- Commit extracted from: `bbfab1bbbe5d4c353241a6df4e7d9a112a1ba356`
  ("docs(managed_agents): remove cost and latency ratios…", 2026-08-28)
- License: MIT, Copyright (c) 2023 Anthropic.

Cell indices below are 0-based positions in the notebook's `cells` array at that commit.

**Why this target.** The section exists to demonstrate `tool_choice={"type": "any"}`: an SMS
bot whose only channel is a tool call, so it must *never* answer in plain text. That is exactly
the parameter Fable 5.1 rejects (DESIGN.md, `api_error_forced_tool_choice`), and here it is not
an incidental setting — it is the whole design of the agent. Every copy of this pattern in the
wild breaks the same way.

## What was extracted, and from where

| Artifact | Source |
| --- | --- |
| `system_prompt.txt` | Cell 33, the `system_prompt = """…"""` literal — verbatim, including its leading and trailing newlines (321 bytes). That string is passed as `system=` in `sms_chatbot`. |
| `tools.json` | Cell 33, the `tools = [...]` literal — the `send_text_to_user` and `get_customer_info` dicts, extracted by `ast.literal_eval` of the cell source rather than retyped. Names, descriptions and schemas are byte-identical, double spaces and the trailing space in `"The username of the user in question. "` included. |
| `agent.json` `model` | Cell 2 sets `MODEL_NAME = "claude-sonnet-4-6"` — see "The model" below. |
| `agent.json` `params` | Cell 33's `client.messages.create(..., max_tokens=4096, tool_choice={"type": "any"}, ...)`. |
| `backend.py` | Cell 33's `send_text_to_user` and `get_customer_info` mock implementations. |
| `cases/cases.json` | The section's own example inputs: cells 35, 37, 39, 41. |

### Tool-schema shape

The notebook writes Anthropic-native tools (`{name, description, input_schema}`).
`tools.json` is chat-style (`{"type":"function","function":{name,description,parameters}}`)
because ADAPTER.md requires that shape for every endpoint; `agent_loop` converts `parameters`
back to `input_schema` before the request goes out, so the wire body matches the notebook. The
conversion is mechanical and lossless — verified against a recorded request.

## The model

`agent.json` sets `claude-fable-5`, the baseline of the migration under test. The notebook runs
`claude-sonnet-4-6` (cell 2). This is a deliberate substitution: the notebook is teaching a
*pattern*, and the pattern is what we are upgrading. No claim is made that Anthropic runs this
notebook on Fable 5.

## Backend: the notebook's own mocks

- `get_customer_info(username)` is copied unchanged: it returns
  `{"username": username, "email": f"{username}@email.com", "purchases": [...]}` with the same
  three hardcoded purchases (computer mouse, screen protector, usb charging cable). It is a
  pure function of its argument, so ADAPTER.md's determinism requirement holds by construction.
- `send_text_to_user(text)` records the text.

### Deltas from the notebook

- **Return value of `send_text_to_user`.** The notebook's function `print`s
  `TEXT MESSAGE SENT: {text}` and returns `None` — it is never fed back to the model, because
  the notebook makes exactly one API call and stops. A tool result here must be a JSON dict, so
  the backend returns `{"result": "TEXT MESSAGE SENT: <text>"}`: the printed line, wrapped.
- **A loop where the notebook has none.** `sms_chatbot` (cell 33) calls the API once, executes
  at most one tool, and prints. upshift runs a real tool-calling loop, so the agent can look a
  customer up *and then* text the answer — the behaviour the notebook narrates in prose ("Claude
  calls the `get_customer_info` tool, just as we hoped!") but never actually completes. Every
  multi-step case here depends on that loop.
- **Only the last content block, upstream.** The notebook inspects `response.content[-1]` and
  ignores any earlier tool_use block. upshift executes every tool call in a turn.
- **`max_turns: 6`.** The notebook has no cap because it has no loop. With `tool_choice: any`
  in force on every turn the model must call a tool every time, so an episode ends at the cap
  rather than on a text reply; six is enough for the longest oracle plan here (four tool turns)
  with headroom.
- **`initial_state` is unused.** The notebook has no session state; every case starts empty.
- **`Backend.state()` is upshift's, not the notebook's.** It exposes `texts_sent`,
  `text_count`, `lookups` and `delivered`.

### `delivered` — a derived projection, stated plainly

`delivered` is `{"username": bool, "email": bool, "purchases": bool}`: for each customer field,
whether a value that some `get_customer_info` call *actually returned this episode* appears
(case-insensitively) in some text the agent sent. It is not something the notebook models.

It exists because the check vocabulary has no substring assertion over state (ADAPTER.md,
"Check types") and, under `tool_choice: any`, the agent never produces a final text message for
`response_contains` to look at — the answer goes out through `send_text_to_user`. The
alternatives were asserting exact message wording (eval overfitting, ruled out in CLAUDE.md's
session log) or asserting nothing about content. So the substring test is computed in the
backend, deterministically, over data the tools themselves produced. It is the same kind of
deliberate normalization as shell_gpt's whitespace-stripped `files_text`, and it is recorded
here for the same reason: a reader must be able to see it and disagree.

## Eval cases

Six cases in `cases/cases.json`. Four use the notebook's example inputs verbatim:

| case | source |
| --- | --- |
| `greeting_texts_back` | cell 35 — `sms_chatbot("Hey there! How are you?")` |
| `order_help_asks_for_username` | cell 37 — `"I need help looking up an order"` |
| `order_help_with_username` | cell 39 — `"I need help looking up an order.  My username is jenny76"` (double space preserved) |
| `gibberish_still_calls_a_tool` | cell 41 — `"askdj aksjdh asjkdbhas kjdhas 1+1 ajsdh"` |
| `email_on_file_requested` | written here: username up front, one named field requested back |
| `username_given_in_second_message` | written here: two segments, the notebook's ask-then-look-up flow completed |

Checks assert tool calls and final state, never the final assistant message — see the
`tool_choice: any` note above. `tool_not_called: get_customer_info` on the first two encodes
the system prompt's own rule ("Do not call the get_customer_info tool until a user has provided
you with their username. This is important.").

Every case carries a `sim.oracle_plan`, so `--provider sim` exercises the pipeline for free.
In those plans the outgoing text is the bare field value (`$ref:get_customer_info[0].email`),
because the sim substitutes `$ref:` only when it is the entire argument value. That is a
property of the simulator, not of the agent.

### Facts we could not verify

- **Star count.** The "52k stars" figure comes from the task brief; this session had no network
  access to confirm it.
- **Whether the notebook's recorded outputs still reproduce.** Cells 35, 37, 39 and 41 *do*
  carry stored stdout from a past run on some earlier model, and it matches what each case
  asserts: cells 35, 37 and 41 show `=======Claude Wants To Call The send_text_to_user
  Tool=======`, and cell 39 shows `=======Claude Wants To Call The get_customer_info
  Tool=======` followed by the jenny76 record. Those are evidence about that run, not about
  Fable 5 — the checks were chosen to match them, and only a real run can confirm them.
- **No response-content assertions.** Because `tool_choice: any` prevents a final text turn, no
  check here asserts anything about the model's prose. If a future patch drops `tool_choice`,
  these cases still hold — the answers still travel through `send_text_to_user`.

## Reproducing

```shell
uv run pytest -q tests/test_agents_claude_a.py
upshift upgrade --agent agents/cookbook-sms --provider sim \
    --baseline-model sim-fable-5 --candidate-model sim-fable-5-1 --tag sms-sim
```

Sim results validate the machinery, never the thesis (DESIGN.md).
