#!/usr/bin/env python3
"""Reference runner: upshift result protocol v1, in Python.

Copy this file into your project, replace `run_agent` with a call to YOUR agent, and add a
`runner` block to `agent.json`. That is the whole integration — see examples/runners/README.md
and ADAPTER.md, "Native runner".

Run it by hand to see the protocol::

    echo '{"protocol":1,"case_id":"demo","rep":1,"seed":7,"model":"stub-model-a",
           "endpoint":"chat_completions","initial_state":{"tasks":[]},
           "user_messages":["Add a task to call the dentist."],"patch_applied":false}' \
      | python examples/runners/python_runner.py

What this particular runner drives is a tiny real agent loop (`fake_provider.py`) against a
stub provider, so the example, and the test that runs it, need no API key and no network. In
your project the loop is your application's, and the provider is the real one — that is the
entire point of a native run: upshift never builds the request.

Two rules the protocol imposes, and this file demonstrates:

1. **One JSON object on stdout, last line.** Everything else goes to stderr. The line below
   that prints progress to stderr is not decoration; a runner that logs to stdout after the
   result is a `runner_error`.
2. **Report an API error as `api_error`, do not exit non-zero.** A 400 from the provider is
   the single most valuable thing upshift can observe (it is what the whole gpt-5.6 and
   Fable 5.1 work is about). Exiting non-zero turns it into a harness failure and upshift will
   correctly refuse to call it evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider import StubProvider

PROTOCOL = 1

TOOLS = [
    {
        "name": "add_task",
        "description": "Add one task to the list.",
        "parameters": {"type": "object", "properties": {"title": {"type": "string"}},
                       "required": ["title"]},
    },
    {
        "name": "list_tasks",
        "description": "List every task.",
        "parameters": {"type": "object", "properties": {}},
    },
]

SYSTEM_PROMPT = "You manage a task list. Use the tools; never claim a task exists unless a tool added it."


class Tools:
    """The application's own tool implementations — upshift's backend.py plays no part here."""

    def __init__(self, initial_state: dict) -> None:
        self.tasks = list(initial_state.get("tasks") or [])

    def execute(self, name: str, arguments: dict) -> dict:
        if name == "add_task":
            title = str(arguments.get("title") or "")
            if not title:
                return {"error": "title is required"}
            self.tasks.append({"title": title, "status": "open"})
            return {"added": title, "count": len(self.tasks)}
        if name == "list_tasks":
            return {"tasks": list(self.tasks)}
        return {"error": f"unknown tool {name}"}

    def state(self) -> dict:
        return {"tasks": list(self.tasks)}


def run_agent(case: dict) -> dict:
    """The application's agent loop. Yours goes here."""
    provider = StubProvider(model=case["model"], endpoint=case["endpoint"], seed=case["seed"])
    tools = Tools(case.get("initial_state") or {})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    tool_executions: list[dict] = []
    api_calls: list[dict] = []
    final_message = ""
    usage = {"input_tokens": 0, "output_tokens": 0}

    for segment, user_message in enumerate(case.get("user_messages") or []):
        messages.append({"role": "user", "content": user_message})
        for turn in range(8):
            request = {"model": case["model"], "messages": messages, "tools": TOOLS}
            try:
                response = provider.call(request)
            except RuntimeError as exc:  # the provider rejected the request (a real 400)
                api_calls.append({"request": request, "response": None,
                                  "error": {"message": str(exc), "status_code": 400}})
                return {
                    "final_message": "",
                    "tool_executions": tool_executions,
                    "final_state": tools.state(),
                    "api_calls": api_calls,
                    "api_error": {"message": str(exc), "status_code": 400,
                                  "type": "invalid_request_error"},
                    "usage": usage,
                }
            api_calls.append({"request": request, "response": response, "error": None})
            for key, value in (response.get("usage") or {}).items():
                usage[key] = usage.get(key, 0) + value
            calls = response.get("tool_calls") or []
            if not calls:
                final_message = str(response.get("text") or "")
                messages.append({"role": "assistant", "content": final_message})
                break
            messages.append({"role": "assistant", "tool_calls": calls})
            for call in calls:
                result = tools.execute(call["name"], call.get("arguments") or {})
                tool_executions.append(
                    {"turn": turn, "segment": segment, "name": call["name"],
                     "arguments": call.get("arguments") or {}, "result": result}
                )
                messages.append({"role": "tool", "content": json.dumps(result)})
            print(f"turn {turn}: {len(calls)} tool call(s)", file=sys.stderr)

    return {
        "final_message": final_message,
        "tool_executions": tool_executions,
        "final_state": tools.state(),
        "api_calls": api_calls,
        "api_error": None,
        "usage": usage,
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        case = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"runner: stdin was not JSON ({exc})", file=sys.stderr)
        return 1
    if case.get("protocol") != PROTOCOL:
        print(f"runner: unsupported protocol {case.get('protocol')!r}", file=sys.stderr)
        return 1
    result = run_agent(case)
    result["protocol"] = PROTOCOL
    print(json.dumps(result))  # the last stdout line, and the only JSON one
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
