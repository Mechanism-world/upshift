"""Rescue regression 5 — `seed` is handled explicitly, and never called "deterministic".

maintenance coverage derived from the rescue campaign's translation findings (finding #2 in
PRODUCT_RELIABILITY_UPGRADE.md) — not independent evidence of general repair capability.

`seed` is the one parameter whose *name* makes a promise the API does not keep. DESIGN §G:
it is passed through where the provider accepts it, dropped and recorded where it does not,
and the record says `determinism: "best_effort"` — never "deterministic". A run record that
claimed determinism would make N-rep statistics look like theatre, and would excuse exactly
the flakiness the whole statistics layer exists to measure.
"""

from __future__ import annotations

import json

import pytest
from _support import dropped_names, translation_report

pytestmark = [pytest.mark.mocked_transport]


def test_seed_is_either_passed_through_or_dropped_but_always_recorded():
    report = translation_report("responses", {"seed": 7})

    mapped = report.get("params") or {}
    dropped = set(dropped_names(report))

    # `passthrough_params` is DESIGN §G's list of params the translation table does NOT know;
    # `seed` is a table row, so a forwarded seed is recorded by its `determinism` note instead.
    if "seed" in mapped:
        assert report.get("determinism") == "best_effort", (
            "a forwarded seed must be recorded as determinism best_effort"
        )
        assert "seed" not in dropped
    else:
        assert "seed" in dropped, "a seed that is not forwarded must be listed in dropped_params"


def test_the_record_calls_it_best_effort_and_never_deterministic():
    report = translation_report("responses", {"seed": 7})

    assert report.get("determinism") == "best_effort", (
        "DESIGN §G: the record says determinism best_effort for a seed, never 'deterministic'"
    )
    assert "deterministic" not in json.dumps(report).replace("best_effort", "")


def test_a_seed_is_dropped_and_recorded_where_the_endpoint_has_no_seed():
    """Anthropic's Messages API has no `seed`; DESIGN §G drops it and says so."""
    report = translation_report("messages", {"seed": 7})

    assert "seed" not in (report.get("params") or {})
    assert "seed" in set(dropped_names(report))
    assert report.get("determinism") != "deterministic"
