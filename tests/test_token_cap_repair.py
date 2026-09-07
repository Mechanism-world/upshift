"""The gpt-5-era token-cap rename: detector + repair candidates.

Regression for a real lab finding (case ghc-223, ardhaecosystem/synapse -> graphiti's
`OpenAIGenericClient`). Every gpt-5-family model on api.openai.com answers a
`chat.completions` call carrying `max_tokens` with

    400 Unsupported parameter: 'max_tokens' is not supported with this model.
        Use 'max_completion_tokens' instead.

Before this, that 400 fell into `api_error_other`, which generates NO candidates: an
8-case suite regressed 8/8 at p=0.00397 and `upshift upgrade` reported STAY PINNED after
screening zero candidates ("no further candidates for the observed failure signatures"),
even though the fix is a one-key edit to `agent.json` params — squarely inside the allowed
`model_params` repair type, and exactly the edit the upstream PR makes.

Everything here is offline: agent dirs are written into tmp_path, records are hand-built.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from upshift.differ import (
    SIG_API_ERROR_OTHER,
    SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS,
    SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP,
    failure_signatures,
)
from upshift.repair.playbook import generate_candidates
from upshift.schemas import AgentConfig, CheckResult, RepRecord

#: The exact string api.openai.com returned on all 50 candidate reps of case ghc-223.
MAX_TOKENS_400 = {
    "message": (
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead."
    ),
    "status_code": 400,
    "type": "api_status_error",
}


def rep(api_error: dict[str, Any]) -> RepRecord:
    return RepRecord(
        case_id="c",
        rep=1,
        seed=1,
        model_requested="gpt-5.6-luna",
        resolved_model="gpt-5.6-luna",
        endpoint="chat_completions",
        params={},
        api_calls=[],
        tool_executions=[],
        final_state={},
        final_message="",
        check_results=[
            CheckResult(check={"type": "no_api_error"}, passed=False, detail="api error")
        ],
        passed=False,
        api_error=api_error,
        usage={"input_tokens": 0, "output_tokens": 0},
        latency_s=0.1,
    )


def write_agent(
    tmp_path: Path,
    *,
    endpoint: str = "chat_completions",
    params: dict[str, Any] | None = None,
    one_line: bool = True,
) -> Path:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "name": "a",
        "endpoint": endpoint,
        "model": "gpt-5.6-luna",
        "params": params if params is not None else {},
        "system_prompt_file": "system_prompt.txt",
        "tools_file": "tools.json",
        "max_turns": 1,
    }
    text = json.dumps(raw) if one_line else json.dumps(raw, indent=2) + "\n"
    (agent_dir / "agent.json").write_text(text)
    (agent_dir / "system_prompt.txt").write_text("You are a helpful assistant.\n")
    (agent_dir / "tools.json").write_text("[]")
    return agent_dir


def only(patches, patch_id):
    matches = [p for p in patches if p.id == patch_id]
    assert matches, f"{patch_id} not generated (got {[p.id for p in patches]})"
    return matches[0]


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def test_the_documented_max_tokens_400_gets_its_own_signature() -> None:
    assert failure_signatures([rep(MAX_TOKENS_400)]) == [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]


@pytest.mark.parametrize(
    "message",
    [
        "Unsupported parameter: 'max_tokens' is not supported with this model.",
        "unsupported parameter: 'MAX_TOKENS' IS NOT SUPPORTED with this model",
        "Unsupported parameter: 'max_completion_tokens' is not supported with this model.",
        "Unsupported parameter: 'max_output_tokens' is not supported with this model.",
    ],
)
def test_every_token_cap_spelling_is_recognised(message: str) -> None:
    err = {"status_code": 400, "message": message, "type": "api_status_error"}
    assert failure_signatures([rep(err)]) == [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]


@pytest.mark.parametrize(
    "err",
    [
        # right words, wrong status code -> not a parameter rejection
        {
            "status_code": 500,
            "message": "'max_tokens' is not supported",
            "type": "api_status_error",
        },
        # merely mentions the cap: a length complaint, not a rename
        {
            "status_code": 400,
            "message": "max_tokens must be an integer greater than 0",
            "type": "api_status_error",
        },
        {
            "status_code": 400,
            "message": "This model's maximum context length is 8192 tokens",
            "type": "api_status_error",
        },
    ],
)
def test_a_cap_mentioned_but_not_rejected_is_not_this_signature(err: dict[str, Any]) -> None:
    assert failure_signatures([rep(err)]) == [SIG_API_ERROR_OTHER]


def test_a_400_naming_temperature_still_reads_as_a_sampling_rejection() -> None:
    """The two OpenAI 400s must not collide: only one signature is ever returned per error."""
    err = {
        "status_code": 400,
        "message": "Unsupported parameter: 'temperature' is not supported with this model.",
        "type": "api_status_error",
    }
    assert failure_signatures([rep(err)]) == [SIG_API_ERROR_UNSUPPORTED_SAMPLING_PARAMS]


# ---------------------------------------------------------------------------
# Repair candidates
# ---------------------------------------------------------------------------


def test_rename_keeps_the_value_and_touches_one_key(tmp_path: Path) -> None:
    agent_dir = write_agent(
        tmp_path,
        params={"temperature": 1, "max_tokens": 16384, "response_format": {"type": "json_object"}},
    )
    patch = only(
        generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]),
        "rename-token-cap-param",
    )
    assert patch.repair_type == "model_params"
    assert [e.file for e in patch.edits] == ["agent.json"]
    patched = json.loads(patch.edits[0].new_content)
    assert patched["params"] == {
        "temperature": 1,
        "max_completion_tokens": 16384,
        "response_format": {"type": "json_object"},
    }
    # nothing else about the agent moved
    original = json.loads((agent_dir / "agent.json").read_text())
    assert {k: v for k, v in patched.items() if k != "params"} == {
        k: v for k, v in original.items() if k != "params"
    }


def test_rename_targets_the_endpoints_own_spelling(tmp_path: Path) -> None:
    agent_dir = write_agent(
        tmp_path, endpoint="responses", params={"max_completion_tokens": 512}
    )
    patch = only(
        generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]),
        "rename-token-cap-param",
    )
    assert json.loads(patch.edits[0].new_content)["params"] == {"max_output_tokens": 512}


def test_the_minimal_edit_rewrites_only_the_key_line(tmp_path: Path) -> None:
    agent_dir = write_agent(tmp_path, params={"max_tokens": 16384}, one_line=False)
    patch = only(
        generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]),
        "rename-token-cap-param",
    )
    before = (agent_dir / "agent.json").read_text().splitlines()
    after = patch.edits[0].new_content.splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    assert '"max_tokens"' in changed[0][0] and '"max_completion_tokens"' in changed[0][1]


def test_the_fallback_candidate_drops_the_cap_entirely(tmp_path: Path) -> None:
    agent_dir = write_agent(tmp_path, params={"max_tokens": 16384, "temperature": 1})
    patch = only(
        generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]),
        "drop-token-cap-param",
    )
    assert json.loads(patch.edits[0].new_content)["params"] == {"temperature": 1}


def test_the_rename_is_ordered_before_the_drop(tmp_path: Path) -> None:
    """The 400 names the replacement, so honouring it comes first; dropping the cap is the
    fallback and loses the author's chosen limit."""
    agent_dir = write_agent(tmp_path, params={"max_tokens": 16384})
    ids = [p.id for p in generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP])]
    assert ids.index("rename-token-cap-param") < ids.index("drop-token-cap-param")


def test_no_candidates_when_the_agent_declares_no_cap(tmp_path: Path) -> None:
    agent_dir = write_agent(tmp_path, params={"temperature": 1})
    assert generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]) == []


def test_no_rename_when_the_agent_already_uses_the_endpoints_spelling(tmp_path: Path) -> None:
    agent_dir = write_agent(tmp_path, params={"max_completion_tokens": 16384})
    ids = [p.id for p in generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP])]
    assert "rename-token-cap-param" not in ids
    # the cap can still be dropped; the 400 came from somewhere
    assert "drop-token-cap-param" in ids


def test_no_rename_when_both_spellings_are_already_declared(tmp_path: Path) -> None:
    """Renaming would overwrite a value the author chose deliberately."""
    agent_dir = write_agent(
        tmp_path, params={"max_tokens": 16384, "max_completion_tokens": 512}
    )
    ids = [p.id for p in generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP])]
    assert "rename-token-cap-param" not in ids


def test_the_patch_applies_and_the_agent_still_loads(tmp_path: Path) -> None:
    agent_dir = write_agent(
        tmp_path, params={"temperature": 1, "max_tokens": 16384}, one_line=False
    )
    patch = only(
        generate_candidates(agent_dir, [SIG_API_ERROR_UNSUPPORTED_TOKEN_CAP]),
        "rename-token-cap-param",
    )
    for edit in patch.edits:
        (agent_dir / edit.file).write_text(edit.new_content)
    config = AgentConfig.load(agent_dir)
    assert config.params["max_completion_tokens"] == 16384
    assert "max_tokens" not in config.params


def test_the_loop_forwards_every_signature_the_differ_can_emit() -> None:
    """The repair loop keeps its own copy of the signature priority order and filters the
    differ's signatures through it before generating candidates. A signature missing from
    that copy is silently dropped: candidates that exist are never even asked for, which is
    how the token-cap repair still produced STAY PINNED after the playbook could fix it.

    `thinking_block_invalid` is the one deliberate omission (the loop refuses it instead).
    """
    from upshift.differ import SIG_THINKING_BLOCK_INVALID, SIGNATURE_PRIORITY
    from upshift.repair.loop import _SIGNATURE_PRIORITY

    assert list(_SIGNATURE_PRIORITY) == [
        s for s in SIGNATURE_PRIORITY if s != SIG_THINKING_BLOCK_INVALID
    ]
