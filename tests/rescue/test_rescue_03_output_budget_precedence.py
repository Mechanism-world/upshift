"""Rescue regression 3 — an explicitly-spelled output budget beats a translated one.

maintenance coverage derived from rescue cases ghi56-001 (Rynaro/prisma) and ghc-062
(Voidious/crispen) — not independent evidence of general repair capability.

DESIGN §G, output cap row: `max_tokens` / `max_completion_tokens` / `max_output_tokens` are
three spellings of one budget, and **the explicit native spelling wins**. If a translated
`max_completion_tokens: 4096` could overwrite an author's `max_output_tokens: 32000`, the
endpoint-routing repair would quietly cap a reasoning model at an eighth of its budget and
the resulting truncation would be scored as a behavioural regression of the candidate model.
"""

from __future__ import annotations

import pytest
from _support import incident

from upshift.agent_loop import map_params

pytestmark = [pytest.mark.mocked_transport]


def test_explicit_native_spelling_wins_over_the_translated_one():
    params = dict(incident("prisma")["params"])
    params["max_output_tokens"] = 32000

    mapped = map_params("responses", params)

    assert mapped["max_output_tokens"] == 32000
    assert "max_completion_tokens" not in mapped


def test_translation_still_fills_in_when_the_native_spelling_is_absent():
    mapped = map_params("responses", {"max_completion_tokens": 4096})
    assert mapped["max_output_tokens"] == 4096


@pytest.mark.parametrize("spelling", ["max_tokens", "max_completion_tokens"])
def test_either_chat_spelling_translates(spelling):
    mapped = map_params("responses", {spelling: 384})
    assert mapped["max_output_tokens"] == 384
    assert spelling not in mapped


def test_crispen_small_budget_survives_the_endpoint_repair():
    crispen = incident("crispen")
    mapped = map_params("responses", crispen["params"])

    assert mapped["max_output_tokens"] == 384
    # The forced choice crispen sends is chat-nested; /v1/responses takes it flat.
    assert mapped["tool_choice"] == {"type": "function", "name": "evaluate_duplicate"}
    assert "function" not in mapped["tool_choice"]


def test_the_chat_endpoint_does_not_invent_a_responses_spelling():
    """Precedence is a translation rule, not a rewrite: chat/completions keeps its own name."""
    mapped = map_params("chat_completions", {"max_completion_tokens": 4096})
    assert mapped == {"max_completion_tokens": 4096}
