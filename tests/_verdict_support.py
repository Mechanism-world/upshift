"""Shared fixtures for the v0.5 verdict-integrity tests (stream 3).

A tiny agent directory and a SCRIPTED provider: unlike the sim provider, which models a
model, this one is a switchboard — the caller says exactly which cases answer correctly under
which system prompt, so a test can construct the situations the verdict rules exist for (a
candidate that fixes one case and breaks another; a candidate that wins its selection runs
and loses the fresh final one) instead of hoping the simulator produces them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from upshift.providers.base import Provider

#: The marker a repair candidate writes into the system prompt. Scripts key off it, which is
#: how a scripted provider expresses "this agent has the patch applied".
PATCH_MARKER = "PATCHED"

BACKEND_PY = '''"""Minimal backend: the scripted tests are about verdicts, not tool semantics."""


class Backend:
    def __init__(self, initial_state):
        self._state = dict(initial_state or {})

    def execute(self, name, arguments):
        return {"ok": True, "name": name, "arguments": arguments}

    def state(self):
        return dict(self._state)


def create_backend(initial_state):
    return Backend(initial_state)
'''


def write_agent(directory: Path, case_ids: list[str], *, prompt: str = "BASE") -> Path:
    """A five-file agent dir whose cases each want the word OK in the final message."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.json").write_text(
        json.dumps(
            {
                "name": "scripted",
                "endpoint": "chat_completions",
                "model": "scripted-base",
                "params": {},
                "system_prompt_file": "prompt.txt",
                "tools_file": "tools.json",
                "max_turns": 3,
            },
            indent=1,
        )
    )
    (directory / "prompt.txt").write_text(prompt + "\n")
    (directory / "tools.json").write_text("[]\n")
    (directory / "backend.py").write_text(BACKEND_PY)
    (directory / "cases").mkdir(exist_ok=True)
    (directory / "cases" / "cases.json").write_text(
        json.dumps(
            [
                {
                    "id": case_id,
                    "description": f"scripted case {case_id}",
                    "initial_state": {},
                    "user_messages": [f"do {case_id}"],
                    "checks": [{"type": "response_contains", "text": "OK"}],
                }
                for case_id in case_ids
            ],
            indent=1,
        )
    )
    return directory


class ScriptedProvider(Provider):
    """Answers "OK" (pass) or "NO" (fail) per (model, patched?, case).

    ``script`` maps a model name to a dict with keys ``base`` and ``patched``, each a set of
    the case ids that PASS in that configuration. ``fail_after`` optionally makes a
    configuration start failing a case after a given number of full passes through it, which
    is how a candidate that wins its selection runs and loses the fresh final one is built.

    ``markers`` names more than one patched configuration: ``{"PATCH_A": "a", "PATCH_B": "b"}``
    makes the system prompt's marker select the script key, which is how SIBLING repair
    candidates — several patches offered for the same signature, each with different effects —
    are expressed. The default is the single ``PATCH_MARKER`` -> ``"patched"`` mapping every
    earlier test was written against.
    """

    name = "sim"  # a simulator: nothing here is evidence about a real model

    def __init__(
        self,
        script: dict[str, dict[str, set[str]]],
        *,
        fail_after: dict[str, int] | None = None,
        api_error: dict[str, Any] | None = None,
        markers: dict[str, str] | None = None,
    ) -> None:
        self.script = script
        self.fail_after = dict(fail_after or {})
        self.api_error = api_error
        self.markers = dict(markers or {PATCH_MARKER: "patched"})
        self.seen: dict[str, int] = {}
        self.requests: list[dict[str, Any]] = []

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.requests.append({"endpoint": endpoint, "request": request, "seed_key": seed_key})
        if self.api_error is not None:
            from upshift.providers.base import ProviderAPIError

            raise ProviderAPIError(
                self.api_error["message"],
                status_code=self.api_error.get("status_code"),
                error_type=self.api_error.get("type", "api_error"),
            )
        case_id = (sim_context or {}).get("case_id") or seed_key.split(":")[0]
        model = request.get("model", "")
        system = "".join(
            str(m.get("content", "")) for m in request.get("messages", []) if m.get("role") == "system"
        )
        variant = next(
            (name for marker, name in self.markers.items() if marker in system), "base"
        )
        passing = set(self.script.get(model, {}).get(variant, set()))
        key = f"{model}:{variant}:{case_id}"
        self.seen[key] = self.seen.get(key, 0) + 1
        limit = self.fail_after.get(key)
        ok = case_id in passing and (limit is None or self.seen[key] <= limit)
        return {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": "OK" if ok else "NO"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
