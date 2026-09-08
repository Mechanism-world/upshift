"""Data contracts for upshift. See DESIGN.md; changes require updating DESIGN.md first.

Everything here must round-trip through JSON (dataclasses.asdict / from_dict) because run
records on disk are the source of truth for every diff and verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Agent config: the patchable surface of the victim agent
# ---------------------------------------------------------------------------

#: `messages` is Anthropic's Messages API (DESIGN.md, "Anthropic provider").
ENDPOINTS = ("chat_completions", "responses", "messages")

#: Verification scope (DESIGN.md §A). Derived from the agent directory, never declared by the
#: user, and the ONLY thing that licenses the phrase "verified in the application".
SCOPE_REQUEST_CONTRACT = "request_contract"
SCOPE_ADAPTED_AGENT = "adapted_agent"
SCOPE_NATIVE_APPLICATION = "native_application"
SCOPES = (SCOPE_REQUEST_CONTRACT, SCOPE_ADAPTED_AGENT, SCOPE_NATIVE_APPLICATION)

#: What a capture-derived episode does when it needs more assistant turns than the recording
#: provided (DESIGN.md §F). Default `fail`, because silently recycling the last recorded turn
#: is how a recording of 2 turns becomes "evidence" about turn 9.
CONTINUATION_FAIL = "fail"
CONTINUATION_REPEAT_LAST = "repeat_last"
CONTINUATIONS = (CONTINUATION_FAIL, CONTINUATION_REPEAT_LAST)


def validate_turn_params(raw: Any, where: str) -> list[dict[str, Any]]:
    """`agent.json`'s optional `turn_params`, checked. Absent -> []; anything else is an
    authoring error, which is a ValueError everywhere in upshift."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(  # noqa: TRY004 - authoring errors are ValueError throughout
            f"{where}: turn_params must be a list of param objects, one per assistant turn "
            f"(got {type(raw).__name__}); see ADAPTER.md"
        )
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(  # noqa: TRY004
                f"{where}: turn_params[{index}] must be an object of param overrides "
                f"(got {type(entry).__name__}); see ADAPTER.md"
            )
    return [dict(entry) for entry in raw]


@dataclass
class AgentConfig:
    """Resolved agent configuration loaded from an agent dir (agent.json +
    system_prompt_file + tools_file)."""

    name: str
    endpoint: str  # one of ENDPOINTS
    model: str
    params: dict[str, Any]  # e.g. {"reasoning_effort": "medium"}; passed through to the API
    system_prompt: str
    tools: list[dict[str, Any]]  # chat/completions-style tool definitions
    max_turns: int
    agent_dir: str  # absolute path of the directory the config was loaded from
    #: Text some agents regenerate and append to the trailing user turn of EVERY request (a
    #: live dynamic-facts block: rescue-ops `cases/A-015/REPORT.md` §4). ONE recorded sample,
    #: appended per request by agent_loop and never stored in the conversation history.
    #: Empty for every agent that does not do this, which is nearly all of them.
    volatile_suffix: str = ""
    #: Tools that END the episode when the model calls one, because the framework the agent
    #: was captured from never returned a result for them (`upshift adapt --from-capture`
    #: derives the list from the capture: a `tool_use` no later request ever answered).
    #: pydantic-ai's `final_result` is the canonical example. Empty for every other agent.
    terminal_tools: list[str] = field(default_factory=list)
    #: Per-turn param overrides, applied over `params` by assistant-turn index; the LAST entry
    #: repeats for every later turn, and a `None` value unsets that param for that turn.
    #: Empty means `params` is what every turn sends, which is what nearly every agent does.
    #: A framework that forces a tool on turn 1 and then goes `auto` (pydantic-ai, litellm)
    #: is a different agent from one that forces on every turn — under a forced choice the
    #: model can never answer in text — so one episode-level value cannot express it.
    turn_params: list[dict[str, Any]] = field(default_factory=list)
    #: The `runner` block (DESIGN.md §C): present ⇒ this agent runs ITSELF and upshift only
    #: supplies the case, the reps and the checks. `system_prompt`, `tools` and `backend.py`
    #: are not used for execution — the application's own code builds the request. Kept as the
    #: raw dict so `agent.json` round-trips; `runner_spec()` is the validated view.
    #: Absent for every adapter agent, which is what makes this backwards-compatible.
    runner: dict[str, Any] | None = None
    #: Capture continuation policy (DESIGN.md §F), read only when `recorded_turns` is set.
    continuation: str = CONTINUATION_FAIL
    #: How many assistant turns the RECORDING that produced this agent actually contained.
    #: Written by `adapt --from-capture`; `None` for a hand-written agent, which is what makes
    #: the continuation policy inert for every agent that was not built from a recording.
    recorded_turns: int | None = None

    def runner_spec(self) -> Any:
        """The validated `runner` block, or None. Raises ValueError on a malformed one."""
        if self.runner is None:
            return None
        from upshift.native.protocol import RunnerSpec

        return RunnerSpec.from_dict(self.runner, f"{self.agent_dir}/agent.json")

    @staticmethod
    def load(agent_dir: str | Path) -> AgentConfig:
        agent_dir = Path(agent_dir)
        path = agent_dir / "agent.json"
        if not path.is_file():
            raise ValueError(f"agent dir {agent_dir} has no agent.json (see ADAPTER.md)")
        raw = json.loads(path.read_text())
        native = raw.get("runner") is not None
        # A native agent (DESIGN.md §C) builds its own requests, so the three patchable files
        # are not part of its contract: `runner` + `cases/` is the whole authoring surface.
        required = ("name", "model") if native else (
            "name", "endpoint", "model", "system_prompt_file", "tools_file"
        )
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"{path} is missing required key(s): {', '.join(missing)}")
        endpoint = raw.get("endpoint", "chat_completions" if native else None)
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unknown endpoint {endpoint!r}")
        continuation = str(raw.get("continuation") or CONTINUATION_FAIL)
        if continuation not in CONTINUATIONS:
            raise ValueError(
                f"{path}: unknown continuation policy {continuation!r} "
                f"(expected one of {CONTINUATIONS}); see ADAPTER.md"
            )
        recorded_turns = raw.get("recorded_turns")
        if recorded_turns is not None and (
            isinstance(recorded_turns, bool) or not isinstance(recorded_turns, int)
            or recorded_turns < 1
        ):
            raise ValueError(f"{path}: `recorded_turns` must be a positive integer or absent")
        prompt_file = raw.get("system_prompt_file")
        tools_file = raw.get("tools_file")
        return AgentConfig(
            name=raw["name"],
            endpoint=endpoint,
            model=raw["model"],
            params=raw.get("params", {}),
            system_prompt=(agent_dir / prompt_file).read_text() if prompt_file else "",
            tools=json.loads((agent_dir / tools_file).read_text()) if tools_file else [],
            max_turns=raw.get("max_turns", 12),
            agent_dir=str(agent_dir),
            volatile_suffix=str(raw.get("volatile_suffix") or ""),
            terminal_tools=[str(name) for name in (raw.get("terminal_tools") or [])],
            turn_params=validate_turn_params(raw.get("turn_params"), str(path)),
            runner=dict(raw["runner"]) if isinstance(raw.get("runner"), dict) else raw.get("runner"),
            continuation=continuation,
            recorded_turns=recorded_turns,
        )

    def file_hashes(self) -> dict[str, str]:
        """sha256 of every patchable file, recorded in run manifests.

        A native agent has only `agent.json` here: its behaviour comes from the application
        checkout, whose identity is the commit sha the run records instead (DESIGN.md §B/§E).
        """
        agent_dir = Path(self.agent_dir)
        raw = json.loads((agent_dir / "agent.json").read_text())
        out = {}
        names = ["agent.json", raw.get("system_prompt_file"), raw.get("tools_file")]
        for rel in [name for name in names if name]:
            out[rel] = hashlib.sha256((agent_dir / rel).read_bytes()).hexdigest()
        return out


# ---------------------------------------------------------------------------
# Eval cases
# ---------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    description: str
    initial_state: dict[str, Any]
    user_messages: list[str]
    checks: list[dict[str, Any]]
    sim: dict[str, Any] = field(default_factory=dict)  # sim-provider oracle only; never checked

    @staticmethod
    def load_all(path: str | Path) -> list[Case]:
        raw = json.loads(Path(path).read_text())
        cases = [Case(**c) for c in raw]
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case ids")
        return cases


def case_set_hash(cases: list[Case]) -> str:
    blob = json.dumps([asdict(c) for c in cases], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Episode transcript (one rep of one case)
# ---------------------------------------------------------------------------


@dataclass
class APICall:
    endpoint: str
    request: dict[str, Any]
    response: dict[str, Any] | None  # verbatim API response, None on error
    error: dict[str, Any] | None = None  # {"status_code": int|None, "message": str, "type": str}


@dataclass
class ToolExecution:
    turn: int  # assistant-call index across the whole episode, 0-based
    segment: int  # user-message segment index, 0-based (checks can scope to final segment)
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class CheckResult:
    check: dict[str, Any]
    passed: bool
    detail: str


@dataclass
class RepRecord:
    case_id: str
    rep: int
    seed: int
    model_requested: str
    resolved_model: str | None  # the `model` field the API actually returned
    endpoint: str
    params: dict[str, Any]
    api_calls: list[APICall]
    tool_executions: list[ToolExecution]
    final_state: dict[str, Any]
    final_message: str
    check_results: list[CheckResult]
    passed: bool
    api_error: dict[str, Any] | None
    usage: dict[str, int]  # accumulated {input_tokens, output_tokens}
    latency_s: float
    #: How this episode's behaviour was produced (DESIGN.md §A companion). One of
    #: `recorded_playback` (the tool backend replays captured results, so nothing about the
    #: real world was exercised), `live_model` (upshift built the requests and a provider
    #: answered) and `native_application` (the application's own entry point ran). Defaults to
    #: `live_model`, which is what every record written before this field existed was.
    episode_source: str = "live_model"
    #: Verification scope of the run this rep belongs to; also written to the manifest.
    scope: str = ""
    #: True when the episode ran past the recorded turns under `continuation: "repeat_last"`
    #: (DESIGN.md §F): the last recorded turn's params were reused, which is a fact about the
    #: evidence, not about the model.
    continuation_used: bool = False
    #: Requests the application itself serialized, recorded by `--capture` on a native run.
    wire_requests: list[dict[str, Any]] = field(default_factory=list)
    #: Native runs only: command, exit code, timing, isolation, stderr tail.
    runner: dict[str, Any] = field(default_factory=dict)
    #: Set on a rep re-run by `--retry-errored`: `{"api_error": <the non-behavioural error
    #: this attempt replaced>}`. Empty on a first attempt. It is what lets a reader of a
    #: resumed run see that a green rep was not green the first time, and why.
    retried_from: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> RepRecord:
        d = dict(d)
        d["api_calls"] = [APICall(**a) for a in d["api_calls"]]
        d["tool_executions"] = [ToolExecution(**t) for t in d["tool_executions"]]
        d["check_results"] = [CheckResult(**c) for c in d["check_results"]]
        return RepRecord(**d)


# ---------------------------------------------------------------------------
# Patches (repair candidates)
# ---------------------------------------------------------------------------


@dataclass
class FileEdit:
    """Whole-file replacement of one patchable victim file (relative path)."""

    file: str
    new_content: str


@dataclass
class Patch:
    id: str
    repair_type: str  # prompt_edit | model_params | tool_schema_edit | endpoint_routing
    signature: str  # failure signature that motivated it
    description: str
    edits: list[FileEdit]


# ---------------------------------------------------------------------------
# Case outcome / diff labels (see DESIGN.md statistics section)
# ---------------------------------------------------------------------------

OUTCOME_PASS = "PASS"
OUTCOME_FAIL = "FAIL"
OUTCOME_FLAKY = "FLAKY"

LABEL_STABLE_PASS = "stable-pass"
LABEL_STABLE_FAIL = "stable-fail"
LABEL_REGRESSED = "regressed"
LABEL_IMPROVED = "improved"
LABEL_FLAKY = "flaky"


def outcome(pass_count: int, n: int, pass_threshold: float = 0.8, fail_threshold: float = 0.4) -> str:
    rate = pass_count / n
    if rate >= pass_threshold:
        return OUTCOME_PASS
    if rate <= fail_threshold:
        return OUTCOME_FAIL
    return OUTCOME_FLAKY


def label(baseline_outcome: str, candidate_outcome: str) -> str:
    if baseline_outcome == OUTCOME_FLAKY or candidate_outcome == OUTCOME_FLAKY:
        return LABEL_FLAKY
    table = {
        (OUTCOME_PASS, OUTCOME_PASS): LABEL_STABLE_PASS,
        (OUTCOME_FAIL, OUTCOME_FAIL): LABEL_STABLE_FAIL,
        (OUTCOME_PASS, OUTCOME_FAIL): LABEL_REGRESSED,
        (OUTCOME_FAIL, OUTCOME_PASS): LABEL_IMPROVED,
    }
    return table[(baseline_outcome, candidate_outcome)]
