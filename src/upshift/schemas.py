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

    @staticmethod
    def load(agent_dir: str | Path) -> AgentConfig:
        agent_dir = Path(agent_dir)
        path = agent_dir / "agent.json"
        if not path.is_file():
            raise ValueError(f"agent dir {agent_dir} has no agent.json (see ADAPTER.md)")
        raw = json.loads(path.read_text())
        missing = [
            key
            for key in ("name", "endpoint", "model", "system_prompt_file", "tools_file")
            if key not in raw
        ]
        if missing:
            raise ValueError(f"{path} is missing required key(s): {', '.join(missing)}")
        if raw["endpoint"] not in ENDPOINTS:
            raise ValueError(f"unknown endpoint {raw['endpoint']!r}")
        return AgentConfig(
            name=raw["name"],
            endpoint=raw["endpoint"],
            model=raw["model"],
            params=raw.get("params", {}),
            system_prompt=(agent_dir / raw["system_prompt_file"]).read_text(),
            tools=json.loads((agent_dir / raw["tools_file"]).read_text()),
            max_turns=raw.get("max_turns", 12),
            agent_dir=str(agent_dir),
            volatile_suffix=str(raw.get("volatile_suffix") or ""),
        )

    def file_hashes(self) -> dict[str, str]:
        """sha256 of every patchable file, recorded in run manifests."""
        agent_dir = Path(self.agent_dir)
        raw = json.loads((agent_dir / "agent.json").read_text())
        out = {}
        for rel in ("agent.json", raw["system_prompt_file"], raw["tools_file"]):
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
