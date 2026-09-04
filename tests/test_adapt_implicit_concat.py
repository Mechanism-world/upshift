"""Prompt reconstruction across Python implicit string concatenation.

`("a. " "b.")` is ONE string to the interpreter: the compiler joins adjacent literals with
nothing at all. Chunks extracted from such an expression must be re-joined the same way, or
the generated agent sends a prompt the upstream agent never sends — while every chunk is
still, correctly, reported as verbatim.
"""

from __future__ import annotations

import ast
from pathlib import Path

from upshift.adapt.generate import generate
from upshift.adapt.verify import verify

ROOT = Path(__file__).resolve().parents[1]
IMPLICIT = ROOT / "tests" / "adapt_fixtures" / "implicit_concat"
HANDROLLED = ROOT / "tests" / "adapt_fixtures" / "handrolled"

CHUNK_A = "You are a screener. "
CHUNK_B = "Return JSON with keys a, b."


def source_value(repo: Path, module: str, name: str) -> str:
    """What Python itself evaluates `name` to in `module` — the oracle for this test."""
    tree = ast.parse((repo / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {module}")


def extraction(chunks: list[dict]) -> dict:
    return {
        "agent_name": "screener",
        "endpoint": {"value": "chat_completions", "citation": "screener.py:22",
                     "status": "found", "note": ""},
        "model": {"value": "gpt-4o-mini", "citation": "screener.py:23", "status": "found",
                  "note": ""},
        "params": {"value": {}, "citation": "", "status": "found", "note": ""},
        "max_turns": {"value": 4, "citation": "", "status": "found", "note": ""},
        "system_prompt": {"status": "found", "note": "", "chunks": chunks},
        "tools": [],
        "cases": [],
        "undetermined": [],
    }


def generated_prompt(repo: Path, chunks: list[dict], out_dir: Path) -> str:
    verification = verify(extraction(chunks), repo)
    generate(verification, out_dir, origin="fixture", commit=None)
    return (out_dir / "system_prompt.txt").read_text()


def test_adjacent_literals_are_joined_the_way_python_joins_them(tmp_path):
    prompt = generated_prompt(
        IMPLICIT,
        [
            {"text": CHUNK_A, "kind": "verbatim", "citation": "screener.py:11", "note": ""},
            {"text": CHUNK_B, "kind": "verbatim", "citation": "screener.py:12", "note": ""},
        ],
        tmp_path / "agent",
    )
    expected = source_value(IMPLICIT, "screener.py", "SYSTEM_PROMPT")
    assert expected == "You are a screener. Return JSON with keys a, b."
    assert prompt == expected + "\n", "a newline was inserted where the source has none"


def test_the_chunks_are_still_verified_verbatim(tmp_path):
    verification = verify(
        extraction(
            [
                {"text": CHUNK_A, "kind": "verbatim", "citation": "screener.py:11", "note": ""},
                {"text": CHUNK_B, "kind": "verbatim", "citation": "screener.py:12", "note": ""},
            ]
        ),
        IMPLICIT,
    )
    chunks = verification.data["system_prompt"]["chunks"]
    assert [c["verified"] for c in chunks] == [True, True]
    assert [c["kind"] for c in chunks] == ["verbatim", "verbatim"]
    assert verification.confidence["system_prompt.txt"] == "high"


def test_separate_literals_still_get_a_newline(tmp_path):
    """FOOTER is its own assignment; upstream joins it with an explicit "\\n" in code, so the
    documented one-chunk-per-line behaviour must survive."""
    prompt = generated_prompt(
        IMPLICIT,
        [
            {"text": CHUNK_A, "kind": "verbatim", "citation": "screener.py:11", "note": ""},
            {"text": CHUNK_B, "kind": "verbatim", "citation": "screener.py:12", "note": ""},
            {"text": "Never answer in prose.", "kind": "verbatim",
             "citation": "screener.py:15", "note": ""},
        ],
        tmp_path / "agent",
    )
    assert prompt == (
        "You are a screener. Return JSON with keys a, b.\nNever answer in prose.\n"
    )


def test_escaped_newlines_inside_one_literal_are_not_doubled(tmp_path):
    """The handrolled fixture writes its prompt as `"line one\\n" "line two\\n"` — chunks
    reported without their trailing newline must still come back one per line, not run
    together."""
    prompt = generated_prompt(
        HANDROLLED,
        [
            {"text": "You are Orderly, a support assistant for an online store.",
             "kind": "verbatim", "citation": "support_agent.py:10", "note": ""},
            {"text": "Look orders up before you answer, and never invent an order id.",
             "kind": "verbatim", "citation": "support_agent.py:11", "note": ""},
        ],
        tmp_path / "agent",
    )
    assert prompt == (
        "You are Orderly, a support assistant for an online store.\n"
        "Look orders up before you answer, and never invent an order id.\n"
    )
