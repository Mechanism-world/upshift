"""Deterministic reimplementation of claudette's documented toolloop example backend.

Upstream is `AnswerDotAI/claudette` @ f157e1d, `nbs/01_toolloop.ipynb`: two module-level
dicts built by `_get_orders_customers()` (cell 9) and three plain Python functions over them
(cell 12) whose schemas claudette derives automatically. The functions are already pure
in-memory state machines, so nothing here is an approximation of side effects — the only
work this file does is give each episode its own copy of the dicts, seeded from the case's
`initial_state` (ADAPTER.md requirement 2).

Mirrored, line for line, from cell 12:

* ``get_customer_info(customer_id)`` -> ``customers.get(customer_id, "Customer not found")``
* ``get_order_details(order_id)``    -> ``orders.get(order_id, "Order not found")``
* ``cancel_order(order_id)``         -> ``False`` when the id is unknown, otherwise sets
  ``orders[order_id]['status'] = 'Cancelled'`` and returns ``True``.

**The aliasing matters.** Upstream builds ``customers`` out of the *same dict objects* that
live in ``orders`` (``orders=[orders['O1'], orders['O2']]``, cell 9), so cancelling O1 is
immediately visible through ``get_customer_info('C1')``. JSON cannot express that sharing, so
each customer's order list is stored as ids and re-materialized from ``orders`` on every read
and in ``state()``. Same observable behavior, expressible in a case file.

Shape delta (identical in kind to agents/shell_gpt and agents/quickstart-agent): claudette
puts the function's return value straight into the ``tool_result`` block, while upshift
JSON-encodes whatever ``execute`` returns, so every reply here is wrapped as
``{"result": <upstream return value>}``. Upstream's own "not found" strings and its ``False``
travel inside ``result``, unchanged; only failures upstream would raise on (unknown tool,
non-object arguments, a missing argument) become ``{"error": ...}`` per the contract.

``initial_state`` schema — the notebook's own data, verbatim::

    {"orders":    {"O1": {"id": "O1", "product": ..., "status": "Shipped"}, ...},
     "customers": {"C1": {"name": ..., "email": ..., "phone": ...,
                          "orders": [{"id": "O1", ...}, ...]}, ...}}

``state()`` returns the same two keys after the episode, with each customer's ``orders`` list
rebuilt from the live ``orders`` dict.
"""

from __future__ import annotations

import copy
from typing import Any

CUSTOMER_NOT_FOUND = "Customer not found"
ORDER_NOT_FOUND = "Order not found"
CANCELLED = "Cancelled"


class Backend:
    """One episode's orders/customers store. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        if not isinstance(state, dict):
            state = {}
        orders = state.get("orders")
        self._orders: dict[str, dict[str, Any]] = (
            {str(k): dict(v) for k, v in orders.items() if isinstance(v, dict)}
            if isinstance(orders, dict)
            else {}
        )
        customers = state.get("customers")
        self._customers: dict[str, dict[str, Any]] = {}
        #: customer id -> the order ids it owns, standing in for upstream's shared dict objects
        self._customer_orders: dict[str, list[str]] = {}
        if isinstance(customers, dict):
            for key, value in customers.items():
                if not isinstance(value, dict):
                    continue
                record = {k: v for k, v in value.items() if k != "orders"}
                ids = [
                    str(o.get("id"))
                    for o in (value.get("orders") or [])
                    if isinstance(o, dict) and o.get("id") is not None
                ]
                self._customers[str(key)] = record
                self._customer_orders[str(key)] = ids

    # -- ADAPTER.md contract ------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            if name == "get_customer_info":
                return self._one_arg(arguments, "customer_id", self._get_customer_info)
            if name == "get_order_details":
                return self._one_arg(arguments, "order_id", self._get_order_details)
            if name == "cancel_order":
                return self._one_arg(arguments, "order_id", self._cancel_order)
            return {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        return {
            "orders": copy.deepcopy(dict(sorted(self._orders.items()))),
            "customers": {
                key: self._customer(key) for key in sorted(self._customers)
            },
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _one_arg(arguments: dict[str, Any], key: str, fn) -> dict[str, Any]:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            return {"error": f"missing required argument: {key}"}
        return {"result": fn(value)}

    def _customer(self, customer_id: str) -> dict[str, Any]:
        """Upstream's customer record, with the live order dicts spliced back in."""
        record = copy.deepcopy(self._customers[customer_id])
        record["orders"] = [
            copy.deepcopy(self._orders[oid])
            for oid in self._customer_orders.get(customer_id, [])
            if oid in self._orders
        ]
        return record

    # -- tools (nbs/01_toolloop.ipynb cell 12) ------------------------------

    def _get_customer_info(self, customer_id: str) -> Any:
        if customer_id not in self._customers:
            return CUSTOMER_NOT_FOUND
        return self._customer(customer_id)

    def _get_order_details(self, order_id: str) -> Any:
        if order_id not in self._orders:
            return ORDER_NOT_FOUND
        return copy.deepcopy(self._orders[order_id])

    def _cancel_order(self, order_id: str) -> bool:
        if order_id not in self._orders:
            return False
        self._orders[order_id]["status"] = CANCELLED
        return True


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state`` (ADAPTER.md)."""
    return Backend(initial_state)
