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
from _support import translation_report

pytestmark = [pytest.mark.mocked_transport]


def test_seed_is_either_passed_through_or_dropped_but_always_recorded():
    report = translation_report("responses", {"seed": 7})

    mapped = report.get("params") or {}
    dropped = set(report.get("dropped_params") or [])
    passthrough = set(report.get("passthrough_params") or [])

    if "seed" in mapped:
        assert "seed" in passthrough, "a forwarded seed must be listed in passthrough_params"
        assert "seed" not in dropped
    else:
        assert "seed" in dropped, "a seed that is not forwarded must be listed in dropped_params"


def test_the_record_calls_it_best_effort_and_never_deterministic():
    report = translation_report("responses", {"seed": 7})

    assert report.get("determinism") == "best_effort", (
        "DESIGN §G: the record says determinism best_effort for a seed, never 'deterministic'"
    )
    assert "deterministic" not in json.dumps(report).replace("best_effort", "")
