"""Backend for the toponymy LiteLLMNamer adapter (case opus47-001).

toponymy's namer has no tools: `LiteLLMNamer._call_llm_with_system_prompt`
(toponymy/llm_wrappers.py:1504-1548) sends one system message and one user message and
reads `response.choices[0].message.content` back as text. `tools.json` is therefore `[]`
and this backend never runs anything -- `execute` exists only to satisfy the ADAPTER.md
contract, and returns the same error dict for every call so a stray tool call would be
visible in the record rather than crashing the episode.

Deterministic by construction: no clock, no network, no randomness, no mutable state.
"""

from __future__ import annotations

from typing import Any


class Backend:
    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        self._state: dict[str, Any] = dict(initial_state or {})

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"error": f"unknown tool {name!r}: this agent defines no tools"}

    def state(self) -> dict[str, Any]:
        return dict(self._state)


def create_backend(initial_state: dict[str, Any]) -> Backend:
    return Backend(initial_state)
