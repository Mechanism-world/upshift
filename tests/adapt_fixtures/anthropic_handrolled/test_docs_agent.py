"""Fixture tests for the docly agent (never collected by upshift's own suite)."""

from docs_agent import search_docs


def test_search_docs_finds_the_wifi_section():
    assert search_docs("office wifi")["results"][0]["section_id"] == "S-3"


def test_search_docs_returns_nothing_for_an_unknown_topic():
    assert search_docs("parking")["results"] == []
