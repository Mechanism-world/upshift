"""Tests for the victim booking backend: id sequencing, seat accounting, every error path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from victim.booking_agent.backend import Backend, create_backend


def flight(fid="UA100", origin="SFO", dest="LAX", date="2026-03-05", dep="07:30", price=118, stops=0, seats=3):
    return {
        "flight_id": fid,
        "origin": origin,
        "destination": dest,
        "date": date,
        "depart_time": dep,
        "price": price,
        "stops": stops,
        "seats_available": seats,
    }


def state(flights=None, bookings=None):
    return {"flights": list(flights or []), "bookings": list(bookings or [])}


# ---------------------------------------------------------------------------
# construction / snapshot
# ---------------------------------------------------------------------------


def test_create_backend_returns_backend_and_snapshot_shape():
    backend = create_backend(state([flight()], []))
    assert isinstance(backend, Backend)
    snapshot = backend.state()
    assert set(snapshot) == {"flights", "bookings"}
    assert snapshot["flights"][0]["flight_id"] == "UA100"
    assert snapshot["bookings"] == []


def test_backend_does_not_mutate_the_caller_initial_state():
    initial = state([flight(seats=3)], [])
    backend = create_backend(initial)
    backend.execute("book_flight", {"flight_id": "UA100", "passenger": "A"})
    assert initial["flights"][0]["seats_available"] == 3


def test_state_snapshot_is_deep_copied():
    backend = create_backend(state([flight()], []))
    snapshot = backend.state()
    snapshot["flights"][0]["seats_available"] = 999
    assert backend.state()["flights"][0]["seats_available"] == 3


def test_empty_initial_state_is_tolerated():
    backend = create_backend({})
    assert backend.state() == {"flights": [], "bookings": []}
    assert backend.execute("search_flights", {"origin": "SFO", "destination": "LAX", "date": "2026-03-05"}) == {
        "results": []
    }


# ---------------------------------------------------------------------------
# search_flights
# ---------------------------------------------------------------------------


def test_search_filters_on_route_and_date_exactly():
    backend = create_backend(
        state(
            [
                flight("UA100", "SFO", "LAX", "2026-03-05"),
                flight("DL220", "SFO", "LAX", "2026-03-06"),
                flight("AS330", "SFO", "SAN", "2026-03-05"),
                flight("B6440", "LAX", "SFO", "2026-03-05"),
            ]
        )
    )
    out = backend.execute("search_flights", {"origin": "SFO", "destination": "LAX", "date": "2026-03-05"})
    assert [f["flight_id"] for f in out["results"]] == ["UA100"]


def test_search_iata_match_is_case_insensitive():
    backend = create_backend(state([flight("UA100", "SFO", "LAX", "2026-03-05")]))
    out = backend.execute("search_flights", {"origin": "sfo", "destination": " lax ", "date": "2026-03-05"})
    assert [f["flight_id"] for f in out["results"]] == ["UA100"]


def test_search_sorts_by_price_then_flight_id():
    backend = create_backend(
        state(
            [
                flight("ZZ900", price=200),
                flight("AA100", price=200),
                flight("MM500", price=90),
            ]
        )
    )
    out = backend.execute("search_flights", {"origin": "SFO", "destination": "LAX", "date": "2026-03-05"})
    assert [f["flight_id"] for f in out["results"]] == ["MM500", "AA100", "ZZ900"]


def test_search_max_price_is_inclusive():
    backend = create_backend(state([flight("A1", price=100), flight("A2", price=150), flight("A3", price=151)]))
    out = backend.execute(
        "search_flights",
        {"origin": "SFO", "destination": "LAX", "date": "2026-03-05", "max_price": 150},
    )
    assert [f["flight_id"] for f in out["results"]] == ["A1", "A2"]


def test_search_nonstop_only_keeps_zero_stop_flights():
    backend = create_backend(state([flight("A1", stops=0), flight("A2", stops=1), flight("A3", stops=2)]))
    out = backend.execute(
        "search_flights",
        {"origin": "SFO", "destination": "LAX", "date": "2026-03-05", "nonstop_only": True},
    )
    assert [f["flight_id"] for f in out["results"]] == ["A1"]

    everything = backend.execute(
        "search_flights",
        {"origin": "SFO", "destination": "LAX", "date": "2026-03-05", "nonstop_only": False},
    )
    assert len(everything["results"]) == 3


def test_search_empty_result_is_not_an_error():
    backend = create_backend(state([flight()]))
    out = backend.execute("search_flights", {"origin": "SFO", "destination": "LAX", "date": "2026-12-25"})
    assert out == {"results": []}
    assert "error" not in out


@pytest.mark.parametrize("missing", ["origin", "destination", "date"])
def test_search_missing_required_argument(missing):
    args = {"origin": "SFO", "destination": "LAX", "date": "2026-03-05"}
    del args[missing]
    out = create_backend(state([flight()])).execute("search_flights", args)
    assert out == {"error": f"missing required argument: {missing}"}


def test_search_blank_required_argument_counts_as_missing():
    out = create_backend(state([flight()])).execute(
        "search_flights", {"origin": "  ", "destination": "LAX", "date": "2026-03-05"}
    )
    assert out == {"error": "missing required argument: origin"}


def test_search_rejects_unparseable_filters():
    backend = create_backend(state([flight()]))
    args = {"origin": "SFO", "destination": "LAX", "date": "2026-03-05"}
    assert backend.execute("search_flights", {**args, "max_price": "cheap"}) == {
        "error": "invalid argument: max_price must be a number"
    }
    assert backend.execute("search_flights", {**args, "nonstop_only": "maybe"}) == {
        "error": "invalid argument: nonstop_only must be a boolean"
    }


def test_search_results_are_copies_of_inventory():
    backend = create_backend(state([flight()]))
    out = backend.execute("search_flights", {"origin": "SFO", "destination": "LAX", "date": "2026-03-05"})
    out["results"][0]["price"] = 1
    assert backend.state()["flights"][0]["price"] == 118


# ---------------------------------------------------------------------------
# book_flight
# ---------------------------------------------------------------------------


def test_booking_ids_start_at_1001_and_increment():
    backend = create_backend(state([flight(seats=5)]))
    first = backend.execute("book_flight", {"flight_id": "UA100", "passenger": "A"})
    second = backend.execute("book_flight", {"flight_id": "UA100", "passenger": "B"})
    assert first["booking_id"] == "UPS-1001"
    assert second["booking_id"] == "UPS-1002"


def test_booking_sequence_starts_after_initial_bookings():
    backend = create_backend(
        state(
            [flight(seats=5)],
            [
                {"booking_id": "UPS-1001", "flight_id": "UA100", "passenger": "A", "status": "confirmed"},
                {"booking_id": "UPS-1002", "flight_id": "UA100", "passenger": "B", "status": "cancelled"},
            ],
        )
    )
    out = backend.execute("book_flight", {"flight_id": "UA100", "passenger": "C"})
    assert out["booking_id"] == "UPS-1003"


def test_book_success_payload_and_state():
    backend = create_backend(state([flight(seats=2)]))
    out = backend.execute("book_flight", {"flight_id": "UA100", "passenger": "Maria Lopez"})
    assert out == {
        "confirmation_id": "UPS-1001",
        "booking_id": "UPS-1001",
        "status": "confirmed",
        "flight_id": "UA100",
        "passenger": "Maria Lopez",
    }
    snapshot = backend.state()
    assert snapshot["flights"][0]["seats_available"] == 1
    assert snapshot["bookings"] == [
        {"booking_id": "UPS-1001", "flight_id": "UA100", "passenger": "Maria Lopez", "status": "confirmed"}
    ]


def test_book_unknown_flight_id():
    backend = create_backend(state([flight()]))
    assert backend.execute("book_flight", {"flight_id": "ZZ999", "passenger": "A"}) == {
        "error": "unknown flight_id: ZZ999"
    }
    assert backend.state()["bookings"] == []


def test_book_sold_out_flight():
    backend = create_backend(state([flight(seats=0)]))
    assert backend.execute("book_flight", {"flight_id": "UA100", "passenger": "A"}) == {
        "error": "no seats available on UA100"
    }
    assert backend.state()["flights"][0]["seats_available"] == 0
    assert backend.state()["bookings"] == []


def test_book_exhausts_last_seat_then_errors():
    backend = create_backend(state([flight(seats=1)]))
    assert "error" not in backend.execute("book_flight", {"flight_id": "UA100", "passenger": "A"})
    assert backend.execute("book_flight", {"flight_id": "UA100", "passenger": "B"}) == {
        "error": "no seats available on UA100"
    }


@pytest.mark.parametrize("missing", ["flight_id", "passenger"])
def test_book_missing_required_argument(missing):
    args = {"flight_id": "UA100", "passenger": "A"}
    del args[missing]
    assert create_backend(state([flight()])).execute("book_flight", args) == {
        "error": f"missing required argument: {missing}"
    }


def test_failed_book_does_not_consume_a_sequence_number():
    backend = create_backend(state([flight(seats=1)]))
    backend.execute("book_flight", {"flight_id": "ZZ999", "passenger": "A"})
    out = backend.execute("book_flight", {"flight_id": "UA100", "passenger": "B"})
    assert out["booking_id"] == "UPS-1001"


# ---------------------------------------------------------------------------
# cancel_booking
# ---------------------------------------------------------------------------


def test_cancel_sets_status_and_restores_the_seat():
    backend = create_backend(state([flight(seats=3)]))
    booked = backend.execute("book_flight", {"flight_id": "UA100", "passenger": "A"})
    assert backend.state()["flights"][0]["seats_available"] == 2

    out = backend.execute("cancel_booking", {"booking_id": booked["booking_id"]})
    assert out == {"booking_id": "UPS-1001", "status": "cancelled"}
    snapshot = backend.state()
    assert snapshot["flights"][0]["seats_available"] == 3
    assert snapshot["bookings"][0]["status"] == "cancelled"


def test_cancel_unknown_booking_id():
    backend = create_backend(state([flight()]))
    assert backend.execute("cancel_booking", {"booking_id": "UPS-9999"}) == {
        "error": "no booking found with id: UPS-9999"
    }


def test_cancel_already_cancelled_booking():
    backend = create_backend(
        state(
            [flight(seats=3)],
            [{"booking_id": "UPS-1001", "flight_id": "UA100", "passenger": "A", "status": "cancelled"}],
        )
    )
    assert backend.execute("cancel_booking", {"booking_id": "UPS-1001"}) == {
        "error": "booking UPS-1001 is already cancelled"
    }
    assert backend.state()["flights"][0]["seats_available"] == 3


def test_cancel_twice_errors_the_second_time_and_restores_one_seat_only():
    backend = create_backend(state([flight(seats=3)]))
    backend.execute("book_flight", {"flight_id": "UA100", "passenger": "A"})
    backend.execute("cancel_booking", {"booking_id": "UPS-1001"})
    second = backend.execute("cancel_booking", {"booking_id": "UPS-1001"})
    assert second == {"error": "booking UPS-1001 is already cancelled"}
    assert backend.state()["flights"][0]["seats_available"] == 3


def test_cancel_missing_required_argument():
    assert create_backend(state([flight()])).execute("cancel_booking", {}) == {
        "error": "missing required argument: booking_id"
    }


def test_cancel_of_a_booking_whose_flight_is_gone_still_succeeds():
    backend = create_backend(
        state([], [{"booking_id": "UPS-1001", "flight_id": "GONE", "passenger": "A", "status": "confirmed"}])
    )
    assert backend.execute("cancel_booking", {"booking_id": "UPS-1001"}) == {
        "booking_id": "UPS-1001",
        "status": "cancelled",
    }


# ---------------------------------------------------------------------------
# execute contract
# ---------------------------------------------------------------------------


def test_unknown_tool_name_is_an_error_not_an_exception():
    assert create_backend(state([flight()])).execute("book_hotel", {"city": "DEN"}) == {
        "error": "unknown tool: book_hotel"
    }


def test_non_dict_arguments_are_an_error():
    out = create_backend(state([flight()])).execute("search_flights", ["SFO", "LAX"])
    assert out["error"].startswith("arguments must be an object")


def test_execute_never_raises_on_junk_arguments():
    backend = create_backend(state([flight()]))
    for name, args in [
        ("search_flights", {"origin": None, "destination": None, "date": None}),
        ("book_flight", {"flight_id": 12345, "passenger": 7}),
        ("cancel_booking", {"booking_id": {"nested": "thing"}}),
        ("", {}),
    ]:
        out = backend.execute(name, args)
        assert isinstance(out, dict)
