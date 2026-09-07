"""Collection rules for the rescue regression suite.

maintenance coverage derived from the 2026-09 migration-rescue campaign — not independent
evidence of general repair capability.

The only job of this file is the guarantee that no rescue test can reach a provider by
accident: a `live` mark is registered (see pyproject `[tool.pytest.ini_options] markers`) but
every `live`-marked test under `tests/rescue/` is skipped unless the run explicitly selects
`-m live`. The suite's shared helpers live in `tests/rescue/_support.py`, not here, so test
modules import them by name instead of relying on conftest's import machinery.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    """`live` tests never run unless explicitly selected with `-m live`."""
    if "live" in str(config.getoption("-m") or ""):
        return
    skip_live = pytest.mark.skip(reason="live tests do not run by default; select with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
