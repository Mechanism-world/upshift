"""Deterministic tool backend for the cookbook SMS chatbot.

Upstream: https://github.com/anthropics/claude-cookbooks @ bbfab1b, `tool_use/tool_choice.ipynb`
cell 33 (the "Any" section), MIT. See ATTRIBUTION.md for every delta from the notebook.

The notebook's two mock implementations, verbatim::

    def send_text_to_user(text):
        # Sends a text to the user
        # We'll just print out the text to keep things simple:
        print(f"TEXT MESSAGE SENT: {text}")


    def get_customer_info(username):
        return {
            "username": username,
            "email": f"{username}@email.com",
            "purchases": [
                {"id": 1, "product": "computer mouse"},
                {"id": 2, "product": "screen protector"},
                {"id": 3, "product": "usb charging cable"},
            ],
        }

Both are pure functions of their argument — no clock, no network, no randomness — so the
ADAPTER.md determinism requirement is satisfied by construction. `initial_state` is unused by
design: the notebook has no per-session state, and every case here starts from the same empty
one.
"""

from __future__ import annotations

import copy
from typing import Any

#: Purchases returned for every username (notebook cell 33, verbatim).
PURCHASES: list[dict[str, Any]] = [
    {"id": 1, "product": "computer mouse"},
    {"id": 2, "product": "screen protector"},
    {"id": 3, "product": "usb charging cable"},
]


def get_customer_info(username: str) -> dict[str, Any]:
    """The notebook's mock lookup, unchanged."""
    return {
        "username": username,
        "email": f"{username}@email.com",
        "purchases": copy.deepcopy(PURCHASES),
    }


class Backend:
    """One episode's state. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        self._initial: dict[str, Any] = state if isinstance(state, dict) else {}
        self._texts_sent: list[str] = []
        self._lookups: list[str] = []
        #: Field values this episode's lookups actually returned, per field.
        self._known: dict[str, list[str]] = {"username": [], "email": [], "purchases": []}

    # -- ADAPTER.md contract ------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call. Never raises (ADAPTER.md requirement 2)."""
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            if name == "send_text_to_user":
                return self._send_text_to_user(arguments)
            if name == "get_customer_info":
                return self._get_customer_info(arguments)
            return {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        """Deterministic snapshot of everything the tools changed.

        `delivered` is a derived projection, not a fact the notebook models: it says, per
        customer field, whether a value some `get_customer_info` call actually returned this
        episode appears in some text the agent sent. The check vocabulary has no substring
        assertion over state (ADAPTER.md, "Check types"), and asserting exact message wording
        would be eval overfitting, so the substring test is computed here, deterministically,
        over data the tools themselves produced.
        """
        out = copy.deepcopy(self._initial)
        out.update(
            {
                "texts_sent": list(self._texts_sent),
                "text_count": len(self._texts_sent),
                "lookups": list(self._lookups),
                "delivered": {field: self._delivered(field) for field in self._known},
            }
        )
        return out

    # -- tools --------------------------------------------------------------

    def _send_text_to_user(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = arguments.get("text")
        if not isinstance(text, str) or not text:
            return {"error": "missing required argument: text"}
        self._texts_sent.append(text)
        # The notebook prints `TEXT MESSAGE SENT: {text}` and returns None; a tool result must
        # be a JSON dict here, so the printed line is what the model gets back.
        return {"result": f"TEXT MESSAGE SENT: {text}"}

    def _get_customer_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        username = arguments.get("username")
        if not isinstance(username, str) or not username:
            return {"error": "missing required argument: username"}
        info = get_customer_info(username)
        self._lookups.append(username)
        self._known["username"].append(info["username"])
        self._known["email"].append(info["email"])
        self._known["purchases"].extend(p["product"] for p in info["purchases"])
        return info

    def _delivered(self, field: str) -> bool:
        """True when some value this episode's lookups returned for `field` appears in some
        text the agent sent. False when nothing was looked up."""
        values = self._known[field]
        if not values:
            return False
        haystack = "\n".join(self._texts_sent).lower()
        return any(value.lower() in haystack for value in values)


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state`` (ADAPTER.md)."""
    return Backend(initial_state)
