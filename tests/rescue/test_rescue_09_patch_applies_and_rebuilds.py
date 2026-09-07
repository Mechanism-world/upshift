"""Rescue regression 9 — the exported patch applies, and rebuilds the verified request.

maintenance coverage derived from every REPAIRED_VERIFIED rescue case (PRODUCT_RELIABILITY_
UPGRADE.md finding #1: "repair verified via reconstructed request != verified in the upstream
application") — not independent evidence of general repair capability.

There are two artifacts at the end of a repair: `runs/<tag>/patched_agent/`, which the loop
actually ran, and `upgrade.patch`, which the operator actually applies. DESIGN §E closes the
gap between them: apply the exported patch to a clean copy of the ORIGINAL agent directory,
rebuild the first request of every case with the real `build_request`, and require it to
equal the request recorded in the run that verified the patch. No API call, no second
request-building path, no "it looked the same".
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from _support import require_module, write_agent_dir

from upshift.agent_loop import build_request
from upshift.differ import diff_runs
from upshift.patch import make_patch
from upshift.providers.sim import SimProvider
from upshift.recorder import load_case_reps, run_dir
from upshift.repair.loop import repair
from upshift.runner import run_suite
from upshift.schemas import AgentConfig, Case
from upshift.verdict import SAFE_WITH_PATCH, decide

pytestmark = [pytest.mark.sim]

CASES = [
    {
        "id": "search_only",
        "description": "Search for a flight.",
        "initial_state": {},
        "user_messages": ["Find me a flight from SFO to JFK."],
        "checks": [{"type": "no_api_error"}, {"type": "tool_called", "name": "search_flights"}],
        "sim": {
            "oracle_plan": [
                {
                    "tool_calls": [
                        {
                            "name": "search_flights",
                            "arguments": {"origin": "SFO", "destination": "JFK"},
                        }
                    ]
                },
                {"final_message": "Two options found."},
            ]
        },
    }
]


@pytest.fixture
def repaired(tmp_path):
    """A complete sim upgrade that ends SAFE WITH PATCH, plus its exported patch."""
    agent = write_agent_dir(
        tmp_path / "agent", endpoint="chat_completions", model="sim-5.5", cases=CASES
    )
    runs = tmp_path / "runs"
    provider = SimProvider()
    run_suite(agent, provider, "r09-baseline", n_reps=5, runs_root=runs, workers=1)
    run_suite(
        agent, provider, "r09-candidate", n_reps=5, runs_root=runs,
        model_override="sim-5.6-sol", workers=1,
    )
    diff = diff_runs(run_dir(runs, "r09-baseline"), run_dir(runs, "r09-candidate"))
    outcome = repair(
        original_agent_dir=agent,
        work_dir=tmp_path / "patched_agent",
        provider=provider,
        candidate_model="sim-5.6-sol",
        baseline_diff=diff,
        n_reps=5,
        runs_root=runs,
        run_prefix="r09",
        budget=4,
        workers=1,
    )
    assert decide(diff, outcome, patch_path="upgrade.patch")["verdict"] == SAFE_WITH_PATCH
    patch_text = make_patch(agent, tmp_path / "patched_agent", rel_prefix="agent")
    assert patch_text, "the accepted repair produced no diff"
    return agent, tmp_path / "patched_agent", runs, outcome, patch_text


def test_the_exported_patch_applies_cleanly_to_the_original_agent(repaired, tmp_path):
    agent, _, _, _, patch_text = repaired
    work = tmp_path / "apply"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    shutil.copytree(agent, work / "agent")
    (work / "upgrade.patch").write_text(patch_text)

    check = subprocess.run(
        ["git", "apply", "--check", "upgrade.patch"],
        cwd=work, capture_output=True, text=True, check=False,
    )
    assert check.returncode == 0, f"git apply --check failed: {check.stderr}"

    applied = subprocess.run(
        ["git", "apply", "upgrade.patch"], cwd=work, capture_output=True, text=True, check=False
    )
    assert applied.returncode == 0, applied.stderr


def test_the_applied_patch_rebuilds_the_request_the_run_verified(repaired, tmp_path):
    agent, _patched_agent, runs, outcome, patch_text = repaired

    work = tmp_path / "rebuild"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    shutil.copytree(agent, work / "agent")
    (work / "upgrade.patch").write_text(patch_text)
    subprocess.run(["git", "apply", "upgrade.patch"], cwd=work, check=True)

    config = AgentConfig.load(work / "agent")
    cases = Case.load_all(work / "agent" / "cases" / "cases.json")
    verify_run = run_dir(runs, outcome.final_verify_run_id)
    assert cases, "fixture precondition: the suite has at least one case to rebuild"
    assert config.endpoint == "responses", (
        "fixture precondition: the accepted repair is the endpoint routing one, so the "
        "rebuilt request must come out of the OTHER endpoint's builder"
    )

    for case in cases:
        recorded = load_case_reps(verify_run, case.id)[0].api_calls[0].request
        rebuilt = build_request(
            config.endpoint,
            "sim-5.6-sol",
            config.params,
            tools=config.tools,
            items=(
                [{"role": "user", "content": case.user_messages[0]}]
                if config.endpoint == "messages"
                else [
                    {"role": "system", "content": config.system_prompt},
                    {"role": "user", "content": case.user_messages[0]},
                ]
            ),
            system=config.system_prompt if config.endpoint == "messages" else None,
            volatile_suffix=config.volatile_suffix,
        )
        assert rebuilt == recorded, (
            f"case {case.id}: the request rebuilt from the EXPORTED patch differs from the "
            f"request the verifying run recorded\n"
            f"rebuilt : {json.dumps(rebuilt, sort_keys=True)}\n"
            f"recorded: {json.dumps(recorded, sort_keys=True)}"
        )


def test_the_product_entry_point_runs_the_same_comparison(repaired):
    """`upshift verify-patch` (DESIGN §E) is the shipped form of the two tests above."""
    module = require_module(
        "upshift.verify_patch",
        "DESIGN §E: `upshift verify-patch` writes verdict.json.patch_verification "
        "{patch_sha256, applies_cleanly, scope, agent_files_sha256|commit, config, commands, "
        "runs, live, evidence_ids}",
    )
    agent, _patched_agent, runs, outcome, patch_text = repaired
    verify_patch = getattr(module, "verify_patch", None)
    assert verify_patch is not None, (
        "interface not yet integrated: upshift.verify_patch.verify_patch"
    )
    block = verify_patch(agent_dir=agent, patch_text=patch_text, run_dir=run_dir(
        runs, outcome.final_verify_run_id))
    assert block["applies_cleanly"] is True
    assert block["scope"] in ("request_contract", "adapted_agent", "native_application")
