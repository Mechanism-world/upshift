"""Deterministic in-memory booking backend for the Skyway victim agent.

The agent loop calls ``create_backend(case.initial_state)`` once per episode and then
``backend.execute(name, arguments)`` for every tool call the model emits. ``execute`` never
raises: a bad tool name, a missing argument, or an impossible operation all come back as
``{"error": "..."}`` so that the transcript records exactly what the agent saw.

State shape (JSON-serializable throughout)::

    {"flights":  [{"flight_id", "origin", "destination", "date", "depart_time",
                   "price", "stops", "seats_available"}],
     "bookings": [{"booking_id", "flight_id", "passenger", "status"}]}

Confirmation ids are ``UPS-<seq>``. The sequence starts at ``1000 + len(initial bookings)``
and advances by one per successful booking, so an episode seeded with no bookings issues
UPS-1001 first.
"""

from __future__ import annotations

import copy
from typing import Any

STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"

_TRUE_STRINGS = {"true", "yes", "1"}
_FALSE_STRINGS = {"false", "no", "0"}


class Backend:
    """One episode's booking store. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        self.flights: list[dict[str, Any]] = list(state.get("flights") or [])
        self.bookings: list[dict[str, Any]] = list(state.get("bookings") or [])
        # Sequence counter: the next booking id is "UPS-<counter + 1>".
        self._seq = 1000 + len(self.bookings)

    # -- public interface ---------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call. Never raises; every failure is a dict with an "error" key."""
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            handler = {
                "search_flights": self._search_flights,
                "book_flight": self._book_flight,
                "cancel_booking": self._cancel_booking,
            }.get(name)
            if handler is None:
                return {"error": f"unknown tool: {name}"}
            return handler(arguments)
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        """Deep-copied snapshot of the whole store, safe to record verbatim."""
        return {
            "flights": copy.deepcopy(self.flights),
            "bookings": copy.deepcopy(self.bookings),
        }

    # -- tools --------------------------------------------------------------

    def _search_flights(self, args: dict[str, Any]) -> dict[str, Any]:
        for required in ("origin", "destination", "date"):
            if _is_missing(args.get(required)):
                return {"error": f"missing required argument: {required}"}

        origin = _as_code(args["origin"])
        destination = _as_code(args["destination"])
        date = str(args["date"]).strip()

        max_price = None
        if not _is_missing(args.get("max_price")):
            max_price = _as_number(args["max_price"])
            if max_price is None:
                return {"error": "invalid argument: max_price must be a number"}

        nonstop_only = False
        if not _is_missing(args.get("nonstop_only")):
            nonstop_only = _as_bool(args["nonstop_only"])
            if nonstop_only is None:
                return {"error": "invalid argument: nonstop_only must be a boolean"}

        results = []
        for flight in self.flights:
            if _as_code(flight.get("origin")) != origin:
                continue
            if _as_code(flight.get("destination")) != destination:
                continue
            if str(flight.get("date", "")).strip() != date:
                continue
            if max_price is not None and _price_of(flight) > max_price:
                continue
            if nonstop_only and int(flight.get("stops", 0) or 0) != 0:
                continue
            results.append(copy.deepcopy(flight))

        results.sort(key=lambda f: (_price_of(f), str(f.get("flight_id", ""))))
        return {"results": results}

    def _book_flight(self, args: dict[str, Any]) -> dict[str, Any]:
        for required in ("flight_id", "passenger"):
            if _is_missing(args.get(required)):
                return {"error": f"missing required argument: {required}"}

        flight_id = str(args["flight_id"]).strip()
        passenger = str(args["passenger"]).strip()

        flight = self._find_flight(flight_id)
        if flight is None:
            return {"error": f"unknown flight_id: {flight_id}"}
        if int(flight.get("seats_available", 0)) <= 0:
            return {"error": f"no seats available on {flight_id}"}

        flight["seats_available"] = int(flight["seats_available"]) - 1
        self._seq += 1
        booking_id = f"UPS-{self._seq}"
        self.bookings.append(
            {
                "booking_id": booking_id,
                "flight_id": flight["flight_id"],
                "passenger": passenger,
                "status": STATUS_CONFIRMED,
            }
        )
        return {
            "confirmation_id": booking_id,
            "booking_id": booking_id,
            "status": STATUS_CONFIRMED,
            "flight_id": flight["flight_id"],
            "passenger": passenger,
        }

    def _cancel_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        if _is_missing(args.get("booking_id")):
            return {"error": "missing required argument: booking_id"}

        booking_id = str(args["booking_id"]).strip()
        booking = self._find_booking(booking_id)
        if booking is None:
            return {"error": f"no booking found with id: {booking_id}"}
        if booking.get("status") == STATUS_CANCELLED:
            return {"error": f"booking {booking_id} is already cancelled"}

        booking["status"] = STATUS_CANCELLED
        flight = self._find_flight(str(booking.get("flight_id", "")))
        if flight is not None:
            flight["seats_available"] = int(flight.get("seats_available", 0)) + 1
        return {"booking_id": booking["booking_id"], "status": STATUS_CANCELLED}

    # -- lookups ------------------------------------------------------------

    def _find_flight(self, flight_id: str) -> dict[str, Any] | None:
        for flight in self.flights:
            if str(flight.get("flight_id", "")).strip().upper() == flight_id.upper():
                return flight
        return None

    def _find_booking(self, booking_id: str) -> dict[str, Any] | None:
        for booking in self.bookings:
            if str(booking.get("booking_id", "")).strip().upper() == booking_id.upper():
                return booking
        return None


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state``."""
    return Backend(initial_state)


# -- argument coercion ------------------------------------------------------


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _as_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().lstrip("$"))
        except ValueError:
            return None
    return None


def _price_of(flight: dict[str, Any]) -> float:
    """Fare as a float; unparseable fares sort first and never crash a search."""
    price = _as_number(flight.get("price"))
    return 0.0 if price is None else price


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None
