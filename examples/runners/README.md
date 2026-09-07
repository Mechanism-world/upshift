# Native runners — let your application run itself

Two reference implementations of **upshift result protocol v1**, one in Python and one in Node.
Both drive a small but real tool-calling loop against a stub provider (`fake_provider.py` /
an inline class in the `.mjs`), so you can run them right now with no API key, no network and
no cost:

```console
$ echo '{"protocol":1,"case_id":"demo","rep":1,"seed":7,"model":"stub-model-a",
         "endpoint":"chat_completions","initial_state":{"tasks":[]},
         "user_messages":["Add a task to call the dentist."],"patch_applied":false}' \
    | python examples/runners/python_runner.py
$ …                                        | node examples/runners/node_runner.mjs
```

Swap `stub-model-a` for `stub-model-b` and the stub rejects a request that carries tools, with
the wording of the documented gpt-5.6 break — the shape a real upgrade regression takes.

## When you want one

When the request that broke is built by your application and not by five adapter files: a
framework you cannot lift out, tools whose semantics are a CAD kernel or a database, or a
TypeScript project (`upshift adapt` writes Python backends). Most such projects already have
the thing upshift needs — a pytest, an `npm test`, a `python -m app.eval` that runs one
scenario end to end. A native runner is a thin adapter over that.

## The integration, in three steps

1. **Copy a reference runner** into your project and replace `run_agent` (Python) / `runAgent`
   (Node) with a call to your own agent, using the case's `model`, `endpoint`, `initial_state`
   and `user_messages`.
2. **Write `agent.json`** with a `runner` block — see ADAPTER.md, "Native runner":

   ```json
   {
     "name": "my-app",
     "model": "claude-fable-5-1",
     "runner": {
       "kind": "command",
       "workdir": ".",
       "command": ["python", "-m", "myapp.upshift_runner"],
       "env": {"MYAPP_MODEL": "{model}"},
       "timeout_s": 120
     }
   }
   ```

3. **Write `cases/cases.json`** — the same case schema as every other agent. That is the whole
   authoring surface: no `system_prompt.txt`, no `tools.json`, no `backend.py`.

Then:

```console
$ upshift run --agent my_agent --run-id baseline --model <old> --allow-runner
$ upshift run --agent my_agent --run-id candidate --model <new> --allow-runner
$ upshift diff baseline candidate
```

## The rules the protocol imposes

| rule | why |
| --- | --- |
| One JSON object, **last line of stdout**. Everything else to stderr. | Your existing command prints its own output; the protocol tolerates that and reads only the result line. |
| A provider error goes in `api_error`, and the process exits **0**. | A 400 is evidence — it is the whole subject of a migration. A non-zero exit means *upshift's harness* failed, and upshift will refuse to score it as a model regression. |
| Deterministic given `initial_state`, `user_messages` and `seed`. | Every case runs N times and the differ compares pass counts. A runner that reads the clock or a live database makes every case flaky and every number meaningless (ADAPTER.md requirement 3). |
| Report tool calls in `tool_executions`. | That is what `tool_called`, `tool_not_called`, `no_tool_calls_after_success` and `turns_at_most` are evaluated against. |
| Report the world in `final_state`. | That is what `final_state` and `state_count` assert on. |

`api_calls` and `usage` are optional. Include them: `api_calls` makes the run inspectable
afterwards, and `usage` is what `upshift cost` prices.

## What upshift does to your machine, exactly

- **Nothing without `--allow-runner`** (or `UPSHIFT_ALLOW_RUNNER=1`). Without it upshift prints
  the argv it would run and exits 2.
- **argv, never a shell.** `command` is a list. There is no shell in this path.
- **A minimal environment**: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` where set,
  your `runner.env`, and the API-key variables of the run's provider — nothing else from your
  environment reaches the child.
- **A fresh copy per rep.** `isolation: "workdir-copy"` (the default) copies `workdir` into a
  temp directory, runs there, and deletes it; `.git`, `node_modules` and cache directories are
  skipped, and symlinks are copied as symlinks rather than followed. `in-place` runs in your
  checkout and needs `--allow-in-place`.
- **A deadline and a size cap.** `timeout_s` kills the whole process group (so `npm test`'s
  workers die with it); output past `max_output_bytes` is truncated and the rep becomes a
  `runner_error`.
- **`--capture`** starts the recorder on loopback only and points the child's provider base URL
  at it, so the requests your application actually serialized are attached to each rep record.

## Files here

| file | what it is |
| --- | --- |
| `python_runner.py` | Python reference runner. Copy and edit `run_agent`. |
| `node_runner.mjs` | Node reference runner, same protocol. Copy and edit `runAgent`. |
| `fake_provider.py` | The stub provider the Python example drives, so the demo costs nothing. |

`tests/test_native_runner.py` runs both of these through `run_suite` end to end.
