"""No tools, so no backend behaviour to model — this agent exists only to put a sampling
parameter on the wire and see what the API says about it.

`tools.json` is `[]` (a legal plain-completion agent), so `execute` is never called; it still
returns an error dict rather than raising, because ADAPTER.md says a backend never raises.
"""

from __future__ import annotations

from typing import Any


class Backend:
    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        self._state = dict(initial_state or {})

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"error": f"this agent exposes no tools; got a call to {name!r}"}

    def state(self) -> dict[str, Any]:
        return dict(self._state)


def create_backend(initial_state: dict[str, Any]) -> Backend:
    return Backend(initial_state)
