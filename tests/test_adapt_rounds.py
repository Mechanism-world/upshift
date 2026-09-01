"""Extraction round 2: following the pointers round 1 handed us.

Round 1 is honest about what it could not see — it returns `undetermined` entries whose
`pointer` says where the answer lives: a file the static ranking never surfaced (a tool
schema in a docstring, a prompt in a module that scores zero), or a line range in a ranked
file that fell between the windows the slicer sent. This module is the proof that the tool
now *looks*, and that it never pays to be shown a page it has already read.

Everything here is offline: the extraction engine takes an injectable `call_model`, so a
scripted list of replies stands in for the provider and no test costs a cent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from upshift import cli
from upshift.adapt import AdaptAborted
from upshift.adapt.extract import (
    _claim_pointers,
    build_round2_messages,
    collect_pointers,
    extract,
    parse_pointer,
    plan_round2,
    resolve_pointer_path,
    resolved_undetermined,
    settled_claims,
)
from upshift.adapt.inventory import (
    POINTER_WHOLE_FILE_LINES,
    POINTER_WINDOW,
    resolve_source,
    slice_pointer,
    subtract_ranges,
    take_inventory,
)
from upshift.adapt.report import rounds_line
from upshift.providers.base import ProviderAPIError

ROOT = Path(__file__).resolve().parents[1]
HANDROLLED = ROOT / "tests" / "adapt_fixtures" / "handrolled"

#: `escalations.py` is the fixture's hidden file: no OpenAI signal, no signal-bearing name,
#: so the ranking scores it zero and only a pointer can pull it into the evidence.
HIDDEN_FILE = "escalations.py"
HIDDEN_TOOL_LINE = 10


def inventory_of(path: Path, tmp_path: Path, **kwargs):
    repo = resolve_source(str(path), tmp_path)
    return repo, take_inventory(repo, **kwargs)


# ---------------------------------------------------------------------------
# Scripted extraction replies
# ---------------------------------------------------------------------------

PROMPT_CHUNKS = [
    {"text": "You are Orderly, a support assistant for an online store.",
     "kind": "verbatim", "citation": "support_agent.py:10", "note": ""},
    {"text": "Keep replies to two sentences.", "kind": "verbatim",
     "citation": "support_agent.py:12", "note": ""},
]

LOOKUP_TOOL = {
    "name": "lookup_order",
    "description": "Look up one order by its id.",
    "parameters": {"type": "object",
                   "properties": {"order_id": {"type": "string"}},
                   "required": ["order_id"]},
    "kind": "verbatim",
    "citation": "support_agent.py:19",
    "backend": {"kind": "lookup", "state_key": "orders", "id_field": "order_id",
                "id_prefix": "", "match_fields": ["order_id"], "text_field": "",
                "citation": "support_agent.py:51", "reason": "pure dict lookup over ORDERS"},
}

ESCALATE_TOOL = {
    "name": "escalate_ticket",
    "description": "Escalate one support ticket to a human queue.",
    "parameters": {"type": "object",
                   "properties": {"ticket_id": {"type": "string"},
                                  "severity": {"type": "string"}},
                   "required": ["ticket_id"]},
    "kind": "verbatim",
    "citation": f"{HIDDEN_FILE}:{HIDDEN_TOOL_LINE}",
    "backend": {"kind": "unclear", "state_key": "", "id_field": "", "id_prefix": "",
                "match_fields": [], "text_field": "",
                "citation": f"{HIDDEN_FILE}:{HIDDEN_TOOL_LINE}",
                "reason": "the human queue is not modelled in this repo"},
}


#: One grounded case, so the generated directory is a complete agent directory in every test
#: here and `validate_agent_dir` has something to accept.
CASES = [
    {"id": "lookup_shipped_order", "description": "README status example.",
     "user_messages": ["Where is order A-1001?"],
     "initial_state": {"orders": [{"order_id": "A-1001", "status": "shipped"}]},
     "expected_tool_calls": [{"name": "lookup_order", "arguments": {"order_id": "A-1001"}}],
     "final_message": "Order A-1001 has shipped.",
     "checks": [{"type": "response_contains", "text": "shipped"}],
     "citation": "README.md:10", "note": ""},
]


def base_extraction(**overrides) -> dict:
    data = {
        "agent_name": "orderly",
        "endpoint": {"value": "chat_completions", "citation": "support_agent.py:74",
                     "status": "found", "note": ""},
        "model": {"value": "gpt-4o-mini", "citation": "support_agent.py:75",
                  "status": "found", "note": ""},
        "params": {"value": {"temperature": 0.2}, "citation": "support_agent.py:78",
                   "status": "found", "note": ""},
        "max_turns": {"value": None, "citation": "", "status": "undetermined", "note": ""},
        "system_prompt": {"status": "found", "note": "", "chunks": list(PROMPT_CHUNKS)},
        "tools": [dict(LOOKUP_TOOL)],
        "cases": json.loads(json.dumps(CASES)),
        "undetermined": [],
        "notes": "",
    }
    data.update(overrides)
    return data


#: What round 1 looks like when it did the right thing: one tool found, and an explicit
#: pointer at the file it could not see.
ROUND1 = base_extraction(
    undetermined=[
        {"what": "the full tool list",
         "pointer": f"{HIDDEN_FILE}:{HIDDEN_TOOL_LINE}",
         "why": "handlers are registered from a module that is not in the evidence"},
        {"what": "max_turns", "pointer": "support_agent.py:71",
         "why": "the while loop has no bound"},
    ],
)

#: What round 2 gives back once it has read the pointed-at file.
ROUND2 = base_extraction(
    tools=[dict(LOOKUP_TOOL), dict(ESCALATE_TOOL)],
    undetermined=[
        {"what": "max_turns", "pointer": "support_agent.py:71",
         "why": "the while loop has no bound"},
    ],
)


def chat_response(content: str) -> dict:
    return {
        "id": "chatcmpl-fake",
        "model": "gpt-5.5-2026-01-01",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    }


def scripted(*replies):
    """A `call_model` returning each reply in turn; an Exception reply is raised instead.

    The last reply repeats, so a test only lists as many replies as it cares about.
    """
    seen: list[dict] = []

    def call_model(request: dict) -> dict:
        seen.append(request)
        reply = replies[min(len(seen), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return chat_response(reply if isinstance(reply, str) else json.dumps(reply))

    call_model.requests = seen  # type: ignore[attr-defined]
    return call_model


# ---------------------------------------------------------------------------
# Pointer parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("chatdbg/tool.py:88", ("chatdbg/tool.py", 88)),
        ("tool.py:88-140", ("tool.py", 88)),  # a range: the start is where to look
        ("tool.py", ("tool.py", None)),  # bare path: tolerated, no line
        ("src/x.py:12 (the docstring below the def)", ("src/x.py", 12)),  # trailing prose
        ("`src/x.py`", ("src/x.py", None)),  # quoted the way a model quotes paths
        ("x.py:notaline", ("x.py", None)),  # junk after the colon, path still usable
        ("", None),
        ("   ", None),
    ],
)
def test_pointer_parsing(pointer, expected):
    assert parse_pointer(pointer) == expected


def test_a_pointer_at_a_file_that_does_not_exist_resolves_to_nothing():
    assert resolve_pointer_path(HANDROLLED, "ghost.py") is None
    assert resolve_pointer_path(HANDROLLED, HIDDEN_FILE) == HIDDEN_FILE


def test_a_pointer_out_of_the_repo_is_never_read(tmp_path):
    """A pointer is model output. It may not become a file read outside the source tree."""
    secret = tmp_path / "secret.txt"
    secret.write_text("shhh\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    assert resolve_pointer_path(repo, "../secret.txt") is None
    assert resolve_pointer_path(repo, str(secret)) is None
    assert resolve_pointer_path(repo, str(repo / "a.py")) == "a.py"  # absolute, but inside


def test_a_pointer_at_an_unreadable_kind_of_file_is_skipped(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "weights.bin").write_bytes(b"\x00\x01\x02")
    assert resolve_pointer_path(repo, "weights.bin") is None


# ---------------------------------------------------------------------------
# Which pointers earn a second round
# ---------------------------------------------------------------------------


def test_pointers_at_unseen_files_are_followed(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    plan = plan_round2(inv, ROUND1)
    assert [(p.path, p.line) for p in plan.followed] == [(HIDDEN_FILE, HIDDEN_TOOL_LINE)]
    assert plan.followed[0].source.startswith("undetermined[0]")
    # support_agent.py was in the evidence, whole: re-showing it settles nothing.
    assert plan.files == [HIDDEN_FILE]
    assert "support_agent.py" in inv.slice_paths


def test_a_pointer_at_lines_already_in_the_evidence_means_no_round_two(tmp_path):
    """The fixture's agent file is small enough that round 1 sliced all of it, so a pointer
    into it names nothing the model has not read."""
    _, inv = inventory_of(HANDROLLED, tmp_path)
    data = base_extraction(undetermined=[
        {"what": "max_turns", "pointer": "support_agent.py:71", "why": "unbounded loop"},
    ])
    assert inv.slice_ranges["support_agent.py"] == [(1, 93)], "the whole file went in"
    plan = plan_round2(inv, data)
    assert plan.slices == []
    assert plan.followed == []
    assert "already contained" in plan.skipped


def test_an_open_claim_citation_counts_as_a_pointer_too(tmp_path):
    """Source (b): not only `undetermined[].pointer` — a tool the model marked `inferred`
    whose citation names source it never saw is the same admission."""
    _, inv = inventory_of(HANDROLLED, tmp_path)
    hidden_but_inferred = dict(ESCALATE_TOOL, kind="inferred")
    data = base_extraction(tools=[dict(LOOKUP_TOOL), hidden_but_inferred])
    plan = plan_round2(inv, data)
    assert [p.path for p in plan.followed] == [HIDDEN_FILE]
    assert "escalate_ticket" in plan.followed[0].source


def test_a_found_claim_is_not_chased(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    data = base_extraction(tools=[dict(LOOKUP_TOOL), dict(ESCALATE_TOOL)])  # kind=verbatim
    assert collect_pointers(data, HANDROLLED) == []
    assert plan_round2(inv, data).slices == []


# ---------------------------------------------------------------------------
# Round-2 evidence: window vs whole file, and the budget
# ---------------------------------------------------------------------------


def test_a_small_file_goes_in_whole():
    text = "\n".join(f"line {i}" for i in range(1, 51))
    pieces = slice_pointer("small.py", text, [12])
    assert [(p.start, p.end) for p in pieces] == [(1, 50)]


def test_a_big_file_is_windowed_generously_around_the_pointed_line():
    text = "\n".join(f"line {i}" for i in range(1, 1001))
    pieces = slice_pointer("big.py", text, [500])
    assert [(p.start, p.end) for p in pieces] == [(500 - POINTER_WINDOW, 500 + POINTER_WINDOW)]
    assert pieces[0].text.splitlines()[0] == f"line {500 - POINTER_WINDOW}"


def test_overlapping_windows_are_merged_and_edges_are_clamped():
    text = "\n".join(f"line {i}" for i in range(1, 1001))
    assert [(p.start, p.end) for p in slice_pointer("big.py", text, [500, 520])] == [(420, 600)]
    assert [(p.start, p.end) for p in slice_pointer("big.py", text, [3, 999])] == [
        (1, 83), (919, 1000)
    ]


def test_a_bare_pointer_at_a_big_file_takes_the_head():
    text = "\n".join(f"line {i}" for i in range(1, 1001))
    pieces = slice_pointer("big.py", text, [])
    assert [(p.start, p.end) for p in pieces] == [(1, POINTER_WHOLE_FILE_LINES)]


def big_repo(tmp_path: Path) -> Path:
    """A repo whose agent file ranks and whose 1000-line helper does not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text(
        "from openai import OpenAI\n"
        'SYSTEM_PROMPT = "You are a helpful assistant that books rooms for a team."\n'
        "def go(c):\n"
        '    return c.chat.completions.create(model="gpt-4o", messages=[], tools=TOOLS)\n'
    )
    (repo / "registry.py").write_text(
        "\n".join(f"VALUE_{i} = {i}" for i in range(1, 1001)) + "\n"
    )
    return repo


def test_round_two_evidence_is_windowed_when_the_pointed_file_is_large(tmp_path):
    repo = big_repo(tmp_path)
    _, inv = inventory_of(repo, tmp_path / "work")
    assert "registry.py" not in inv.slice_paths, "the fixture must be invisible to ranking"
    data = base_extraction(undetermined=[
        {"what": "tool list", "pointer": "registry.py:500", "why": "not in the evidence"},
    ])
    plan = plan_round2(inv, data)
    assert plan.files == ["registry.py"]
    assert [(s.start, s.end) for s in plan.slices] == [(420, 580)]
    assert plan.evidence_tokens > 0


# ---------------------------------------------------------------------------
# Ranked is not read: the gaps between a file's round-1 windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("windows", "covered", "expected"),
    [
        ([(20, 180)], [(3, 77), (172, 300)], [(78, 171)]),  # the ChatDBG shape
        ([(1, 100)], [], [(1, 100)]),  # nothing read yet
        ([(1, 100)], [(1, 100)], []),  # fully covered
        ([(1, 100)], [(1, 40), (60, 200)], [(41, 59)]),  # one hole in the middle
        ([(50, 100)], [(1, 200)], []),  # swallowed whole
        ([(1, 10)], [(4, 6)], [(1, 3), (7, 10)]),  # covered range inside the window
        ([(1, 100)], [(1, 40), (44, 100)], [(41, 43)]),  # a 3-line hole is close enough
    ],
)
def test_uncovered_range_arithmetic(windows, covered, expected):
    assert subtract_ranges(windows, covered) == expected


def test_a_seen_file_contributes_only_the_part_of_the_window_it_never_showed():
    text = "\n".join(f"line {i}" for i in range(1, 401))
    pieces = slice_pointer("harness.py", text, [100], covered=[(3, 77), (172, 300)])
    assert [(p.start, p.end) for p in pieces] == [(78, 171)]


def test_a_seen_file_whose_window_was_fully_shown_contributes_nothing():
    text = "\n".join(f"line {i}" for i in range(1, 401))
    assert slice_pointer("harness.py", text, [100], covered=[(1, 300)]) == []


def inventory_with_gap(tmp_path, *, n_lines: int = 400, shown=((3, 77), (172, 300))):
    """A repo whose one ranked file went into the evidence as two windows with a gap.

    This is the shape `upshift adapt` actually produced for ChatDBG: `assistant.py` ranked
    first and was sliced 3-77 and 172-300, so the model never read 78-171 — where the
    function-registration implementation it kept pointing at lives.
    """
    from upshift.adapt.inventory import Inventory, Slice, estimate_tokens

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    text = "\n".join(f"# harness.py line {i}" for i in range(1, n_lines + 1)) + "\n"
    (repo_dir / "harness.py").write_text(text)
    (repo_dir / "handlers.py").write_text(
        '"""Where the concrete definitions live. Never ranked, never sliced."""\n'
        "def do_thing(x):\n    return x\n"
    )
    repo = resolve_source(str(repo_dir), tmp_path / "work")
    lines = text.splitlines()
    slices = [
        Slice(path="harness.py", start=a, end=b, text="\n".join(lines[a - 1 : b]))
        for a, b in shown
    ]
    inventory = Inventory(
        repo=repo, files=[], call_sites=[], prompt_constants=[], slices=slices,
        evidence_tokens=sum(estimate_tokens(s.text) + 16 for s in slices), scanned_files=2,
    )
    return repo_dir, inventory


def test_a_pointer_into_a_gap_of_a_ranked_file_fires_round_two(tmp_path):
    """(a) The case the ChatDBG run exposed: the pointed-at file was ranked, but the lines
    the pointer is about fell between its round-1 windows."""
    _, inventory = inventory_with_gap(tmp_path)
    assert inventory.slice_ranges == {"harness.py": [(3, 77), (172, 300)]}
    data = base_extraction(undetermined=[
        {"what": "concrete tool schemas", "pointer": "harness.py:100",
         "why": "registration happens here but the definitions are not in the evidence"},
    ])
    plan = plan_round2(inventory, data)

    assert plan.files == ["harness.py"]
    assert [(s.start, s.end) for s in plan.slices] == [(78, 171)]
    body = plan.slices[0].text.splitlines()
    assert body[0] == "# harness.py line 78"
    assert body[-1] == "# harness.py line 171"
    # ONLY the gap: nothing round 1 already showed is paid for a second time.
    assert "line 77" not in plan.slices[0].text
    assert "line 172" not in plan.slices[0].text
    assert len(body) == 171 - 78 + 1


def test_a_pointer_into_a_window_the_first_round_covered_fires_nothing(tmp_path):
    """(b) Same file, a pointer whose whole window was already shown."""
    _, inventory = inventory_with_gap(tmp_path, shown=((3, 77), (172, 400)))
    data = base_extraction(undetermined=[
        {"what": "the loop bound", "pointer": "harness.py:300", "why": "unbounded"},
    ])
    plan = plan_round2(inventory, data)
    assert plan.slices == []
    assert plan.followed == []
    assert "already contained" in plan.skipped
    assert [p.path for p in plan.pointers] == ["harness.py"], "it resolved, it just told us "\
        "nothing new"


def test_mixed_pointers_merge_per_path(tmp_path):
    """(c) One unseen file and one gap in a seen file, plus a second pointer into the same
    gap: each path contributes once, with its windows merged."""
    _, inventory = inventory_with_gap(tmp_path)
    data = base_extraction(undetermined=[
        {"what": "schemas", "pointer": "harness.py:100", "why": "gap"},
        {"what": "handlers", "pointer": "handlers.py", "why": "never ranked"},
        {"what": "more schemas", "pointer": "harness.py:130", "why": "the same gap"},
    ])
    plan = plan_round2(inventory, data)
    assert plan.files == ["harness.py", "handlers.py"]
    assert [(s.path, s.start, s.end) for s in plan.slices] == [
        ("harness.py", 78, 171),  # (20,180) and (50,210) merged, minus what was shown
        ("handlers.py", 1, 3),  # unseen and small: the whole file
    ]
    assert len(plan.followed) == 3


CHATDBG_REP = ROOT / "runs" / "adapt-chatdbg" / "cases" / "extraction" / "rep_01.json"


@pytest.mark.skipif(not CHATDBG_REP.is_file(), reason="the ChatDBG evidence run is not on disk")
def test_the_recorded_chatdbg_round_one_would_now_fire_a_second_round():
    """The live-evaluation case, replayed from its own record.

    Round 1 ranked `assistant.py` first and sliced it 3-77 and 172-300, then pointed four
    times at lines whose definitions live in the 78-171 gap. Under the old file-level rule
    nothing fired (the file *was* in the evidence); the window rule reads exactly the gap.
    """
    record = json.loads(CHATDBG_REP.read_text())
    request = record["api_calls"][0]["request"]["messages"][1]["content"]
    covered: dict[str, list[tuple[int, int]]] = {}
    for path, start, end in re.findall(r"===== FILE (\S+) lines (\d+)-(\d+)", request):
        covered.setdefault(path, []).append((int(start), int(end)))

    data = json.loads(record["final_message"])
    lines = sorted({
        parse_pointer(raw)[1]
        for raw, _ in _claim_pointers(data)
        if parse_pointer(raw) and parse_pointer(raw)[0].endswith("assistant/assistant.py")
        and parse_pointer(raw)[1] is not None
    })
    assert lines == [34, 48, 179, 284], "the pointers the real extraction actually returned"

    shown = covered["src/chatdbg/assistant/assistant.py"]
    assert sorted(shown) == [(3, 77), (172, 300)]
    windows = [(max(1, line - POINTER_WINDOW), line + POINTER_WINDOW) for line in lines]
    gaps = subtract_ranges(windows, shown)
    assert (78, 171) in gaps, "the unread gap where _add_function and the schemas live"


def test_round_two_evidence_is_appended_to_the_same_budget_not_a_new_one(tmp_path):
    """The cap an operator agreed to covers both rounds: round 2 spends what is left."""
    _, inv = inventory_of(HANDROLLED, tmp_path, max_tokens=40)
    assert inv.truncated is True
    plan = plan_round2(inv, ROUND1)
    assert plan.slices == []
    assert plan.pointers and plan.pointers[0].path == HIDDEN_FILE
    assert "evidence budget" in plan.skipped
    assert plan.dropped_slices >= 1


def test_the_round_two_prompt_carries_the_previous_json_and_only_new_source(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    plan = plan_round2(inv, ROUND1)
    messages = build_round2_messages(ROUND1, plan.slices)
    system, user = messages[0]["content"], messages[1]["content"]
    assert "NEVER invent" in system  # the same hard rules
    assert "You previously returned the JSON above." not in user
    assert "You previously returned the JSON below." in user
    assert '"the full tool list"' in user  # round 1's own reply, verbatim
    assert f"===== FILE {HIDDEN_FILE} lines 1-" in user
    assert "keep honest undetermined entries" in user
    # Round-1 evidence is NOT repeated: that is the expensive half of the request.
    assert "SYSTEM_PROMPT = (" not in user


# ---------------------------------------------------------------------------
# The two-round flow
# ---------------------------------------------------------------------------


def test_round_two_settles_what_round_one_pointed_at(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(ROUND1, ROUND2)
    result = extract(inv, call_model=call_model, model="gpt-5.5")

    assert result.ok is True
    assert result.rounds == 2
    assert [a.index for a in result.attempts] == [1, 2]
    assert [t["name"] for t in result.data["tools"]] == ["lookup_order", "escalate_ticket"]
    round2 = result.round2
    assert round2.used is True
    assert round2.files == [HIDDEN_FILE]
    assert round2.settled == ["tool:escalate_ticket (new)"]
    assert round2.resolved_undetermined == ["the full tool list"]
    assert round2.first_attempt == 2
    # Round 2 is a fresh request, not a continuation of round 1's message list.
    assert len(call_model.requests) == 2
    assert len(call_model.requests[1]["messages"]) == 2


def test_no_pointer_means_no_second_call(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(base_extraction())
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert len(call_model.requests) == 1
    assert result.rounds == 1
    assert result.round2.ran is False
    assert result.round2.skipped


def test_second_round_can_be_turned_off(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(ROUND1)
    result = extract(inv, call_model=call_model, model="gpt-5.5", second_round=False)
    assert len(call_model.requests) == 1
    assert result.round2 is None
    assert result.rounds == 1


def test_round_two_gets_exactly_one_schema_retry(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(ROUND1, {"agent_name": "orderly"}, ROUND2)
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert [a.index for a in result.attempts] == [1, 2, 3]
    assert [a.ok for a in result.attempts] == [True, False, True]
    assert result.round2.used is True
    assert [t["name"] for t in result.data["tools"]] == ["lookup_order", "escalate_ticket"]
    # The retry shows round 2 its own reply and the exact violations, exactly as round 1 does.
    retry = call_model.requests[2]["messages"]
    assert retry[-2]["role"] == "assistant"
    assert "did not satisfy the schema" in retry[-1]["content"]


def test_two_round_two_violations_leave_round_one_standing(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(ROUND1, {"agent_name": "orderly"})
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert len(call_model.requests) == 3  # round 1, then round 2 twice
    assert result.ok is True, "round 1 was valid and is still what we have"
    assert result.rounds == 1
    assert [t["name"] for t in result.data["tools"]] == ["lookup_order"]
    assert result.round2.ran is True
    assert result.round2.used is False
    assert result.round2.errors


def test_a_blown_budget_mid_round_two_falls_back_to_round_one(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(ROUND1, AdaptAborted("over --max-cost-usd 2.00", stage="extract"))
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert result.ok is True
    assert result.rounds == 1
    assert [t["name"] for t in result.data["tools"]] == ["lookup_order"]
    assert result.round2.aborted == "over --max-cost-usd 2.00"
    assert result.round2.used is False
    assert len(result.attempts) == 1, "the refused call never happened"


def test_an_api_failure_in_round_two_does_not_destroy_round_one(tmp_path):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    call_model = scripted(ROUND1, ProviderAPIError("boom", status_code=500))
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert result.ok is True
    assert result.rounds == 1
    assert "boom" in result.round2.aborted


def test_round_two_still_runs_when_round_one_was_only_salvage(tmp_path):
    """A rejected round 1 that still parsed can carry a pointer; follow it anyway."""
    _, inv = inventory_of(HANDROLLED, tmp_path)
    partial = {"agent_name": "orderly", "undetermined": ROUND1["undetermined"]}
    call_model = scripted(partial, partial, ROUND2)
    result = extract(inv, call_model=call_model, model="gpt-5.5")
    assert [a.ok for a in result.attempts] == [False, False, True]
    assert result.ok is True, "round 2 fixed what two round-1 attempts could not"
    assert result.rounds == 2
    assert [t["name"] for t in result.data["tools"]] == ["lookup_order", "escalate_ticket"]


# ---------------------------------------------------------------------------
# What moved (report inputs)
# ---------------------------------------------------------------------------


def test_settled_claims_names_what_moved_to_found():
    before = base_extraction(
        model={"value": None, "citation": "", "status": "undetermined", "note": ""},
        tools=[dict(LOOKUP_TOOL, kind="inferred")],
    )
    after = base_extraction(tools=[dict(LOOKUP_TOOL), dict(ESCALATE_TOOL)])
    assert settled_claims(before, after) == [
        "model", "tool:lookup_order", "tool:escalate_ticket (new)"
    ]
    assert settled_claims(after, after) == []


def test_resolved_undetermined_is_what_round_one_asked_and_round_two_answered():
    assert resolved_undetermined(ROUND1, ROUND2) == ["the full tool list"]
    assert resolved_undetermined(ROUND2, ROUND1) == []


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ((ROUND1, ROUND2), "**Extraction rounds**: 2"),
        ((base_extraction(),), "no second round"),
        ((ROUND1, AdaptAborted("over --max-cost-usd 2.00")), "round 2 was aborted"),
        ((ROUND1, {"agent_name": "x"}), "never satisfied the schema"),
    ],
)
def test_the_rounds_line_states_what_happened(tmp_path, replies, expected):
    _, inv = inventory_of(HANDROLLED, tmp_path)
    result = extract(inv, call_model=scripted(*replies), model="gpt-5.5")
    line = rounds_line(result)
    assert expected in line
    assert line.startswith("- **Extraction rounds**:")


# ---------------------------------------------------------------------------
# Through the CLI: generation and the report use round 2, and cost prices both
# ---------------------------------------------------------------------------


class SequencedProvider:
    name = "openai"

    def __init__(self, *replies) -> None:
        self.replies = replies
        self.calls: list[dict] = []

    def call(self, endpoint, request, seed_key, sim_context=None):
        self.calls.append(request)
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return chat_response(reply if isinstance(reply, str) else json.dumps(reply))


def run_cli(tmp_path, monkeypatch, provider, name="orderly"):
    monkeypatch.setattr(cli, "_make_provider", lambda args: provider)
    out_dir = tmp_path / name
    code = cli.main([
        "adapt", str(HANDROLLED), "--out", str(out_dir),
        "--runs-root", str(tmp_path / "runs"),
    ])
    return code, out_dir


def test_generation_uses_round_two_and_the_report_says_so(tmp_path, monkeypatch):
    provider = SequencedProvider(ROUND1, ROUND2)
    code, out_dir = run_cli(tmp_path, monkeypatch, provider)
    assert code == 0
    assert len(provider.calls) == 2

    tools = json.loads((out_dir / "tools.json").read_text())
    assert [t["function"]["name"] for t in tools] == ["lookup_order", "escalate_ticket"]

    report = (out_dir / "ADAPT_REPORT.md").read_text()
    assert "**Extraction rounds**: 2" in report
    assert f"`{HIDDEN_FILE}`" in report  # which file round 2 added
    assert "1 claim(s) settled: tool:escalate_ticket (new)" in report
    assert "closed 1 open question(s)" in report

    # The tool that only round 2 could see still went through the verification gate: its
    # name and description really are at the cited line of the pointed-at file.
    provenance = json.loads((out_dir / "PROVENANCE.json").read_text())
    assert provenance["confidence"]["tools.json"] == "high"


def test_both_rounds_land_in_one_run_that_upshift_cost_prices(tmp_path, monkeypatch):
    from upshift.pricing import run_cost

    provider = SequencedProvider(ROUND1, {"agent_name": "x"}, ROUND2)
    code, _ = run_cli(tmp_path, monkeypatch, provider)
    assert code == 0

    run_dir = tmp_path / "runs" / "adapt-orderly"
    reps = sorted((run_dir / "cases" / "extraction").glob("rep_*.json"))
    assert [p.name for p in reps] == ["rep_01.json", "rep_02.json", "rep_03.json"]
    assert [json.loads(p.read_text())["passed"] for p in reps] == [True, False, True]
    priced = run_cost(run_dir)
    assert priced["input_tokens"] == 3000, "every round-2 attempt is priced too"
    assert cli.main(["cost", "adapt-orderly", "--runs-root", str(tmp_path / "runs")]) == 0


def budget_that_allows_only_round_one(tmp_path) -> float:
    """A `--max-cost-usd` between what the guard projects for round 1 and for round 2.

    Derived rather than hardcoded so that editing a prompt moves the number instead of
    breaking the test for the wrong reason.
    """
    from upshift.adapt.extract import build_messages
    from upshift.adapt.record import OUTPUT_ALLOWANCE_TOKENS
    from upshift.pricing import price

    _, inv = inventory_of(HANDROLLED, tmp_path / "budget")
    plan = plan_round2(inv, ROUND1)

    def projected(messages) -> float:
        return price(
            "openai", "gpt-5.5", len(json.dumps(messages)) // 4, OUTPUT_ALLOWANCE_TOKENS, 0
        )

    first = projected(build_messages(inv, None))
    spent = price("openai", "gpt-5.5", 1000, 200, 0)  # the scripted reply's recorded usage
    second = spent + projected(build_round2_messages(ROUND1, plan.slices))
    assert first < second
    return round((first + second) / 2, 6)


def test_a_round_two_abort_is_reported_but_still_ships_round_ones_agent(tmp_path, monkeypatch):
    """The budget stops the extra round, not the run: the directory is round 1's, complete,
    and the report says which round produced it."""
    budget = budget_that_allows_only_round_one(tmp_path)
    provider = SequencedProvider(ROUND1, ROUND2)
    monkeypatch.setattr(cli, "_make_provider", lambda args: provider)
    out_dir = tmp_path / "orderly"
    code = cli.main([
        "adapt", str(HANDROLLED), "--out", str(out_dir),
        "--runs-root", str(tmp_path / "runs"), "--max-cost-usd", str(budget),
    ])
    assert code == 0
    assert len(provider.calls) == 1, "the second round was priced and refused"
    cli.validate_agent_dir(out_dir)
    tools = json.loads((out_dir / "tools.json").read_text())
    assert [t["function"]["name"] for t in tools] == ["lookup_order"]
    report = (out_dir / "ADAPT_REPORT.md").read_text()
    assert "**Extraction rounds**: 1" in report
    assert "round 2 was aborted" in report
    assert "round 1's extraction was used" in report
    assert "## ABORTED" not in report, "round 1 finished; the run is not partial"
