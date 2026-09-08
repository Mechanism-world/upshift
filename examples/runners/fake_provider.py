"""A provider stub, so the reference runners (and their tests) need no key and no network.

It is a stand-in for the real API in exactly two ways that matter for a demonstration:

* `stub-model-a` answers a request with tools by calling `add_task` once and then answering in
  text — a small but real tool-calling episode.
* `stub-model-b` REJECTS a request that carries tools, with the wording of the documented
  gpt-5.6 break (DESIGN.md, "Verified external facts"). That is the shape a real upgrade
  regression takes, so a test can drive baseline → candidate → `runner_error`-free regression
  without spending anything.

Nothing here is evidence about anything. The honesty rule for the simulator applies
unchanged: a stub's response to a repair is true by construction.
"""

from __future__ import annotations

from typing import Any

MODEL_OK = "stub-model-a"
MODEL_BREAKS_ON_TOOLS = "stub-model-b"

TOOLS_REJECTED = (
    "Function tools with reasoning_effort are not supported on this endpoint; "
    "use /v1/responses or set reasoning_effort to 'none'"
)


class StubProvider:
    def __init__(self, model: str, endpoint: str = "chat_completions", seed: int = 0) -> None:
        self.model = model
        self.endpoint = endpoint
        self.seed = seed
        self._turn = 0

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        tools = request.get("tools") or []
        if self.model == MODEL_BREAKS_ON_TOOLS and tools and self.endpoint == "chat_completions":
            raise RuntimeError(TOOLS_REJECTED)
        self._turn += 1
        usage = {"input_tokens": 100, "output_tokens": 20}
        if self._turn == 1 and tools:
            title = _first_user_text(request)
            return {
                "model": self.model,
                "tool_calls": [{"id": "call_1", "name": "add_task", "arguments": {"title": title}}],
                "text": "",
                "usage": usage,
            }
        return {"model": self.model, "tool_calls": [], "text": f"Added: {_first_user_text(request)}",
                "usage": usage}


def _first_user_text(request: dict[str, Any]) -> str:
    for message in request.get("messages") or []:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""
