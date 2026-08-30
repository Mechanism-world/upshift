"""Deterministic in-memory backend for the todo agent test fixture.

This agent exists to prove upshift is not wired to the booking victim: a different domain, a
different tool set (two tools), a different identifier format (``TSK-<seq>``) and a different
state shape (``{"tasks": [...]}``). It follows the ADAPTER.md contract exactly — ``execute``
never raises and reports every failure as ``{"error": ...}``, and the whole backend is a pure
function of ``initial_state`` plus the sequence of calls.
"""

from __future__ import annotations

import copy
from typing import Any

STATUS_OPEN = "open"
STATUS_DONE = "done"


class Backend:
    """One episode's task list. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        self.tasks: list[dict[str, Any]] = list(state.get("tasks") or [])
        # Next task id is "TSK-<counter + 1>", so an empty list issues TSK-1001 first.
        self._seq = 1000 + len(self.tasks)

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            handler = {"list_tasks": self._list_tasks, "add_task": self._add_task}.get(name)
            if handler is None:
                return {"error": f"unknown tool: {name}"}
            return handler(arguments)
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        return {"tasks": copy.deepcopy(self.tasks)}

    # -- tools --------------------------------------------------------------

    def _list_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        status = args.get("status")
        if status is not None and str(status).strip():
            wanted = str(status).strip().lower()
            if wanted not in (STATUS_OPEN, STATUS_DONE):
                return {"error": f"invalid argument: status must be open or done, got {status!r}"}
            results = [t for t in self.tasks if str(t.get("status", "")).lower() == wanted]
        else:
            results = list(self.tasks)
        return {"results": copy.deepcopy(results)}

    def _add_task(self, args: dict[str, Any]) -> dict[str, Any]:
        title = args.get("title")
        if title is None or not str(title).strip():
            return {"error": "missing required argument: title"}
        due = str(args.get("due") or "").strip()

        self._seq += 1
        task_id = f"TSK-{self._seq}"
        task = {
            "task_id": task_id,
            "title": str(title).strip(),
            "due": due,
            "status": STATUS_OPEN,
        }
        self.tasks.append(task)
        return {"task_id": task_id, "title": task["title"], "status": STATUS_OPEN}


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state``."""
    return Backend(initial_state)
