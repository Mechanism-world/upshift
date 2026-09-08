"""Helpers shared by the rescue regression suite.

maintenance coverage derived from the 2026-09 migration-rescue campaign — not independent
evidence of general repair capability.

Three rules hold for everything under `tests/rescue/`:

1. **No live calls.** Every test is marked `sim`, `mocked_transport` or `recorded_fixture`.
   A `live` mark is registered but never runs by default (see `conftest.py`).
2. **No product stubs.** Where DESIGN §v0.5 specifies an interface that is not yet in this
   worktree, the test imports defensively and skips, naming the exact symbol it expects, so
   the integration owner can un-skip it. It never fakes the behaviour it is meant to pin.
3. **Fixtures are configurations, not source.** `fixtures/incidents.json` carries API request
   parameters only; `fixtures/LICENSES.md` records where each incident's license was read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]



def incidents() -> dict[str, Any]:
    return json.loads((FIXTURES / "incidents.json").read_text())


def incident(name: str) -> dict[str, Any]:
    data = incidents()[name]
    assert isinstance(data, dict), f"incident {name!r} is not an object"
    return data


# ---------------------------------------------------------------------------
# Defensive access to v0.5 interfaces owned by the other streams
# ---------------------------------------------------------------------------


def require_attr(module: Any, name: str, design_ref: str) -> Any:
    """Return `module.name`, or skip naming the exact missing symbol."""
    value = getattr(module, name, None)
    if value is None:
        module_name = getattr(module, "__name__", str(module))
        pytest.skip(
            f"interface not yet integrated: {module_name}.{name} ({design_ref})"
        )
    return value


def require_module(dotted: str, design_ref: str) -> Any:
    import importlib

    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError:
        pytest.skip(f"interface not yet integrated: {dotted} ({design_ref})")


def translation_report(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """The DESIGN §G translation record for one params dict.

    Accepts either spelling the translation stream may land on:
    `agent_loop.translate_params(endpoint, params)` returning a mapping/dataclass with the
    mapped request fields, `dropped_params` and `passthrough_params`, or `map_params(endpoint,
    params, report=<dict>)` filling that dict. Skips, naming both, when neither exists.

    Normalised to one shape for the tests: `params` is the mapped request body fields (the
    landed spelling of `Translation.request_fields`), `dropped_params` keeps the DESIGN §G
    element shape `{name, reason}`, and `dropped_param_names` is the same list flattened to
    names for the membership assertions.
    """
    from upshift import agent_loop

    translate = getattr(agent_loop, "translate_params", None)
    if translate is not None:
        result = translate(endpoint, params)
        report = dict(vars(result)) if not isinstance(result, dict) else dict(result)
        report.setdefault("params", report.get("request_fields") or {})
        report["dropped_param_names"] = dropped_names(report)
        return report

    report: dict[str, Any] = {}
    try:
        mapped = agent_loop.map_params(endpoint, params, report=report)  # type: ignore[call-arg]
    except TypeError:
        pytest.skip(
            "interface not yet integrated: upshift.agent_loop.translate_params "
            "(DESIGN §G: table-driven map_params recording dropped_params / "
            "passthrough_params / determinism; alternative accepted spelling: "
            "upshift.agent_loop.map_params(endpoint, params, report=dict))"
        )
    report.setdefault("params", mapped)
    report["dropped_param_names"] = dropped_names(report)
    return report


def dropped_names(report: dict[str, Any]) -> list[str]:
    """The names in a translation record's `dropped_params`, whose DESIGN §G element shape is
    `{"name": ..., "reason": ...}` (a bare string is accepted too)."""
    out: list[str] = []
    for entry in report.get("dropped_params") or []:
        out.append(str(entry.get("name")) if isinstance(entry, dict) else str(entry))
    return out


# ---------------------------------------------------------------------------
# Small agent directories
# ---------------------------------------------------------------------------


BOOKING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": "Search for flights.",
            "parameters": {
                "type": "object",
                "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_flight",
            "description": "Book a flight.",
            "parameters": {"type": "object", "properties": {"flight_id": {"type": "string"}}},
        },
    },
]

BACKEND_SOURCE = '''
"""Deterministic in-memory backend for the rescue regression agent."""


class Backend:
    def __init__(self, initial_state=None):
        self.bookings = {}
        self._seq = 0

    def execute(self, name, arguments):
        if name == "search_flights":
            return {"flights": [{"id": "F1", "price": 250}, {"id": "F2", "price": 310}]}
        if name == "book_flight":
            self._seq += 1
            confirmation = "UPS-%d" % self._seq
            self.bookings[confirmation] = dict(arguments)
            return {"confirmation_id": confirmation, "status": "confirmed"}
        return {"error": "unknown tool %s" % name}

    def state(self):
        return {"bookings": dict(self.bookings)}


def create_backend(initial_state):
    return Backend(initial_state)
'''

SEARCH_PLAN = [
    {
        "tool_calls": [
            {"name": "search_flights", "arguments": {"origin": "SFO", "destination": "JFK"}}
        ]
    },
    {"final_message": "I found two flights for you."},
]


def write_agent_dir(
    path: Path,
    *,
    endpoint: str = "chat_completions",
    model: str = "sim-5.5",
    params: dict[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
    max_turns: int = 12,
    turn_params: list[dict[str, Any]] | None = None,
) -> Path:
    """A minimal but complete five-file agent directory."""
    path.mkdir(parents=True, exist_ok=True)
    agent: dict[str, Any] = {
        "name": "rescue-regression-agent",
        "endpoint": endpoint,
        "model": model,
        "params": params or {},
        "system_prompt_file": "system_prompt.txt",
        "tools_file": "tools.json",
        "max_turns": max_turns,
    }
    if turn_params:
        agent["turn_params"] = turn_params
    (path / "agent.json").write_text(json.dumps(agent, indent=2) + "\n")
    (path / "system_prompt.txt").write_text(
        "You are a flight booking assistant. Help the user book travel.\n"
    )
    (path / "tools.json").write_text(json.dumps(BOOKING_TOOLS, indent=2) + "\n")
    (path / "backend.py").write_text(BACKEND_SOURCE)
    (path / "cases").mkdir(exist_ok=True)
    (path / "cases" / "cases.json").write_text(json.dumps(cases or default_cases(), indent=2))
    return path


def default_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "search_sfo_jfk",
            "description": "Search for a flight.",
            "initial_state": {},
            "user_messages": ["Find me a flight from SFO to JFK."],
            "checks": [
                {"type": "no_api_error"},
                {"type": "tool_called", "name": "search_flights"},
            ],
            "sim": {"oracle_plan": SEARCH_PLAN},
        }
    ]
