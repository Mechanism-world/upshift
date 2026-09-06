"""The framework mapping: the static table, and the report section built from it.

The point of these tests is that the mapping cannot rot quietly. A repair upshift can emit
must reach a knob category; a framework the recorder can detect must have a row; and a knob a
framework does not have must come out of the renderer as "not mapped", never as a guess and
never as silence.

Offline, no key, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from upshift.capture import detect, mapping
from upshift.differ import DiffResult
from upshift.patch import make_patch
from upshift.report import diff_to_markdown, framework_mapping_lines

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / mapping.DOC_PATH


# ---------------------------------------------------------------------------
# The table and the doc say the same thing
# ---------------------------------------------------------------------------

#: The eight frameworks the mapping is required to cover (the spec's list).
REQUIRED = (
    "anthropic-sdk-python",
    "anthropic-sdk-typescript",
    "pydantic-ai",
    "litellm",
    "langchain-anthropic",
    "vercel-ai-sdk",
    "claude-agent-sdk",
    "opencode",
)


def test_every_required_framework_has_a_row() -> None:
    assert sorted(mapping.FRAMEWORKS) == sorted(REQUIRED)


@pytest.mark.parametrize("framework", REQUIRED)
def test_the_doc_names_every_framework_the_table_maps(framework: str) -> None:
    assert f"| {framework} |" in DOC.read_text()


@pytest.mark.parametrize("framework", REQUIRED)
def test_every_framework_covers_every_knob_category(framework: str) -> None:
    """A missing category would render as "not mapped" without anyone having decided that."""
    categories = {mapping.TOOL_CHOICE, mapping.SAMPLING, mapping.EFFORT,
                  mapping.SYSTEM_PROMPT, mapping.TOOLS}
    assert categories <= set(mapping.FRAMEWORKS[framework])


@pytest.mark.parametrize("framework", REQUIRED)
def test_every_cell_carries_a_citation(framework: str) -> None:
    for category, (change, citation) in mapping.FRAMEWORKS[framework].items():
        assert change.strip(), f"{framework}/{category} has no text"
        assert citation.strip(), f"{framework}/{category} has no citation"


def test_a_detectable_framework_is_always_a_mapped_framework() -> None:
    """`upshift capture` must never name a framework the report cannot then map."""
    assert {name for name, _, _ in detect.RULES} <= set(mapping.FRAMEWORKS)


def test_not_mapped_is_stated_rather_than_guessed() -> None:
    change, citation = mapping.FRAMEWORKS["claude-agent-sdk"][mapping.TOOL_CHOICE]
    assert change.startswith(mapping.NOT_MAPPED)
    assert citation  # the absence itself was checked somewhere, and says where


# ---------------------------------------------------------------------------
# Every repair upshift can emit reaches a category
# ---------------------------------------------------------------------------


def _playbook_patch_ids() -> set[str]:
    """The patch ids the repair playbook can emit, read out of its own source."""
    source = (ROOT / "src" / "upshift" / "repair" / "playbook.py").read_text()
    return set(re.findall(r'^\s+add\(\s*\n\s+"([a-z0-9-]+)",', source, re.MULTILINE)) | set(
        re.findall(r'^\s+"([a-z0-9-]+)",\n\s+"(?:model_params|prompt_edit|tool_schema_edit'
                   r'|endpoint_routing)",', source, re.MULTILINE)
    )


def test_the_playbook_ids_this_test_finds_are_the_ones_the_table_names() -> None:
    """Guards the regex above: if it stops finding ids, the next test passes vacuously."""
    found = _playbook_patch_ids()
    assert "remove-forced-tool-choice" in found
    assert "prompt-execute-dont-ask" in found
    assert len(found) >= 12


@pytest.mark.parametrize("repair_type", ["model_params", "prompt_edit", "tool_schema_edit",
                                         "endpoint_routing"])
def test_every_repair_type_reaches_a_category(repair_type: str) -> None:
    assert mapping.categories_for("a-patch-id-nobody-listed", repair_type) or (
        repair_type == "model_params"
    )


def test_every_model_params_patch_id_is_classified() -> None:
    """`model_params` covers three unrelated settings, so its ids must be named one by one."""
    for patch_id in _playbook_patch_ids():
        if patch_id.startswith(("prompt-", "tool-schema-")):
            continue
        assert patch_id in mapping.PATCH_CATEGORIES, f"{patch_id} would render as unclassified"


def test_a_repair_with_no_knob_still_produces_a_row() -> None:
    rows = mapping.rows("claude-agent-sdk", [{"id": "unheard-of", "repair_type": "unheard-of"}])
    assert rows == [("unheard-of", "unclassified", mapping.NOT_MAPPED, mapping.DOC_PATH)]


def test_the_forced_tool_choice_repair_maps_to_both_knobs_it_touches() -> None:
    rows = mapping.rows(
        "pydantic-ai", [{"id": "remove-forced-tool-choice", "repair_type": "model_params"}]
    )
    assert [r[1] for r in rows] == ["forced tool_choice", "system prompt"]
    assert "NativeOutput" in rows[0][2]
    assert "_tool_choice.py:99-101" in rows[0][3]


def test_an_unknown_framework_maps_to_nothing_rather_than_to_anything() -> None:
    rows = mapping.rows(
        "some-framework-we-never-read",
        [{"id": "drop-sampling-params", "repair_type": "model_params"}],
    )
    assert rows == [("drop-sampling-params", "temperature / top_p / top_k",
                     mapping.NOT_MAPPED, mapping.DOC_PATH)]


# ---------------------------------------------------------------------------
# framework_of: only a capture-derived directory has one
# ---------------------------------------------------------------------------


def _agent_json(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "agent.json").write_text(json.dumps(payload))
    return tmp_path


def test_framework_of_reads_the_capture_block(tmp_path: Path) -> None:
    directory = _agent_json(tmp_path, {"capture": {"framework": "litellm"}})
    assert mapping.framework_of(directory) == "litellm"


@pytest.mark.parametrize("payload", [
    {},                                    # a hand-written agent dir
    {"capture": {"framework": ""}},        # captured, nothing detected
    {"capture": {"framework": "unknown"}}, # detected as nothing in particular
    {"capture": "not-a-dict"},
])
def test_framework_of_is_none_when_nothing_was_recorded(tmp_path: Path, payload: dict) -> None:
    assert mapping.framework_of(_agent_json(tmp_path, payload)) is None


def test_framework_of_survives_a_missing_or_unreadable_agent_json(tmp_path: Path) -> None:
    assert mapping.framework_of(tmp_path) is None
    (tmp_path / "agent.json").write_text("{not json")
    assert mapping.framework_of(tmp_path) is None


# ---------------------------------------------------------------------------
# The report section
# ---------------------------------------------------------------------------

def _diff_result() -> DiffResult:
    """The smallest diff a report can be rendered from; the mapping section does not read it."""
    manifest = {
        "provider": "anthropic",
        "n_reps": 3,
        "thresholds": {"pass": 0.8, "fail": 0.4},
        "agent": {"model_requested": "claude-fable-5", "endpoint": "messages"},
    }
    return DiffResult(
        baseline_run_id="b",
        candidate_run_id="c",
        baseline_manifest=dict(manifest),
        candidate_manifest=dict(manifest),
        cases=[],
        counts={},
    )


VERDICT = {
    "verdict": "SAFE WITH PATCH",
    "framework": "pydantic-ai",
    "accepted_patches": [
        {"id": "remove-forced-tool-choice", "repair_type": "model_params",
         "description": "drop it"},
    ],
}


def test_the_section_is_rendered_with_its_citations() -> None:
    lines = framework_mapping_lines(None, VERDICT)
    text = "\n".join(lines)
    assert text.startswith("## Framework mapping")
    assert "pydantic-ai" in text
    assert "NativeOutput" in text
    assert mapping.DOC_PATH in text


def test_an_explicit_framework_beats_the_one_in_the_verdict() -> None:
    text = "\n".join(framework_mapping_lines("litellm", VERDICT))
    assert "drop `tool_choice=` from `completion()`" in text


@pytest.mark.parametrize("verdict", [
    None,
    {"accepted_patches": []},
    {"framework": "unknown", "accepted_patches": VERDICT["accepted_patches"]},
    {"accepted_patches": VERDICT["accepted_patches"]},  # no framework: no section
])
def test_no_framework_and_no_repair_means_no_section(verdict: dict | None) -> None:
    assert framework_mapping_lines(None, verdict) == []


def test_the_markdown_report_carries_the_section() -> None:
    text = diff_to_markdown(_diff_result(), verdict=VERDICT)
    assert "## Framework mapping" in text
    assert "| repair | knob | change | verified at |" in text


def test_the_markdown_report_omits_it_without_a_framework() -> None:
    plain = dict(VERDICT, framework=None)
    assert "Framework mapping" not in diff_to_markdown(_diff_result(), verdict=plain)


# ---------------------------------------------------------------------------
# The patch header
# ---------------------------------------------------------------------------


def test_the_patch_header_is_comment_lines_only() -> None:
    header = mapping.patch_header("opencode", VERDICT["accepted_patches"])
    assert header
    assert all(line.startswith("#") for line in header.splitlines())
    assert mapping.NOT_MAPPED in header  # opencode's tool_choice is not a config key
    assert header.endswith("\n")


def test_no_framework_means_no_header() -> None:
    assert mapping.patch_header(None, VERDICT["accepted_patches"]) == ""
    assert mapping.patch_header("pydantic-ai", []) == ""


def test_the_header_sits_above_the_first_diff_line_so_git_still_applies(tmp_path: Path) -> None:
    original, patched = tmp_path / "a", tmp_path / "b"
    for directory, prompt in ((original, "before"), (patched, "after")):
        directory.mkdir()
        (directory / "agent.json").write_text(json.dumps(
            {"system_prompt_file": "system_prompt.txt", "tools_file": "tools.json"}
        ))
        (directory / "system_prompt.txt").write_text(prompt + "\n")
        (directory / "tools.json").write_text("[]\n")
    text = make_patch(original, patched, rel_prefix="agent",
                      header=mapping.patch_header("litellm", VERDICT["accepted_patches"]))
    before, _, _ = text.partition("diff --git ")
    assert before, "the header is missing"
    assert all(line.startswith("#") for line in before.splitlines())
    assert "litellm" in before


def test_an_empty_diff_stays_empty_even_with_a_header(tmp_path: Path) -> None:
    """A patch file is written only when there is something to apply."""
    directory = tmp_path / "a"
    directory.mkdir()
    (directory / "agent.json").write_text(json.dumps(
        {"system_prompt_file": "system_prompt.txt", "tools_file": "tools.json"}
    ))
    (directory / "system_prompt.txt").write_text("same\n")
    (directory / "tools.json").write_text("[]\n")
    assert make_patch(directory, directory, rel_prefix="agent", header="# header\n") == ""
