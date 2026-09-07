"""DESIGN.md §B — evidence identity: stale results cannot be silently reused.

The failure this prevents is the cheapest one to commit and the most expensive to discover:
edit the prompt, re-run under the same `--tag`, and the runner resumes — half the reps
answered the old question, half the new one, and the diff reports the average as a result.
rescue-ops A-075 §6.3 records the family this belongs to: an input changing underneath a run
without the record saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _verdict_support import ScriptedProvider, write_agent

from upshift import recorder
from upshift.providers.sim import SimProvider
from upshift.runner import run_suite

CASES = ["c1", "c2"]


def _agent(tmp_path: Path) -> Path:
    return write_agent(tmp_path / "agent", CASES)


def _provider() -> ScriptedProvider:
    return ScriptedProvider({"m": {"base": set(CASES), "patched": set(CASES)}})


def _run(agent_dir: Path, runs: Path, provider) -> Path:
    return run_suite(
        agent_dir, provider, "tag", n_reps=2, model_override="m", runs_root=runs, workers=1
    )


def _manifest(runs: Path) -> dict:
    return json.loads((runs / "tag" / "manifest.json").read_text())


# ---------------------------------------------------------------------------
# evidence_id_for: what is and is not an input
# ---------------------------------------------------------------------------

BASE_INPUTS = {
    "upshift_version": "0.5.0",
    "provider": "openai",
    "endpoint": "chat_completions",
    "model_requested": "gpt-5.6-sol",
    "file_hashes": {"agent.json": "aa", "prompt.txt": "bb", "tools.json": "cc"},
    "n_reps": 5,
    "thresholds": {"pass": 0.8, "fail": 0.4},
    "scope": "adapted_agent",
}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("upshift_version", "0.5.1"),
        ("provider", "anthropic"),
        ("endpoint", "responses"),
        ("model_requested", "gpt-5.6-luna"),
        ("file_hashes", {"agent.json": "aa", "prompt.txt": "XX", "tools.json": "cc"}),
        ("n_reps", 10),
        ("thresholds", {"pass": 0.6, "fail": 0.4}),
        ("scope", "native_application"),
    ],
)
def test_every_documented_input_changes_the_evidence_id(field, value) -> None:
    before = recorder.evidence_id_for(**BASE_INPUTS)
    after = recorder.evidence_id_for(**{**BASE_INPUTS, field: value})
    assert before != after, f"{field} is not an input to the evidence id"


def test_a_patch_makes_a_different_experiment() -> None:
    unpatched = recorder.evidence_id_for(**BASE_INPUTS)
    patched = recorder.evidence_id_for(**BASE_INPUTS, patch_sha256="deadbeef")
    assert unpatched != patched


def test_identical_inputs_give_an_identical_id() -> None:
    assert recorder.evidence_id_for(**BASE_INPUTS) == recorder.evidence_id_for(**BASE_INPUTS)


def test_file_hash_order_does_not_matter() -> None:
    reordered = dict(reversed(list(BASE_INPUTS["file_hashes"].items())))
    assert recorder.evidence_id_for(**BASE_INPUTS) == recorder.evidence_id_for(
        **{**BASE_INPUTS, "file_hashes": reordered}
    )


# ---------------------------------------------------------------------------
# The resume guard
# ---------------------------------------------------------------------------


def test_the_manifest_records_an_evidence_id_and_the_agent_dir(tmp_path: Path) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    manifest = _manifest(runs)
    assert manifest["evidence_id"] == recorder.evidence_id_of(manifest)
    assert Path(manifest["agent_dir"]) == agent_dir.resolve()
    assert manifest["scope"] == recorder.SCOPE_ADAPTED_AGENT


def test_an_unchanged_run_resumes(tmp_path: Path) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    first = _manifest(runs)["created_at"]
    provider = _provider()
    _run(agent_dir, runs, provider)
    # Resumed, not re-run: every rep was already complete, so no call was made.
    assert provider.requests == []
    assert _manifest(runs)["created_at"] == first


@pytest.mark.parametrize("target", ["prompt.txt", "tools.json", "agent.json"])
def test_one_changed_byte_of_an_agent_file_refuses_the_resume(tmp_path: Path, target) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    path = agent_dir / target
    if target == "agent.json":
        raw = json.loads(path.read_text())
        raw["max_turns"] = raw["max_turns"] + 1
        path.write_text(json.dumps(raw, indent=1))
    else:
        path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="different evidence_id") as exc:
        _run(agent_dir, runs, _provider())
    assert "file_hashes" in str(exc.value)


def test_a_changed_n_reps_refuses_the_resume(tmp_path: Path) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    with pytest.raises(ValueError, match="different evidence_id") as exc:
        run_suite(
            agent_dir, _provider(), "tag", n_reps=3, model_override="m", runs_root=runs, workers=1
        )
    assert "n_reps" in str(exc.value)


def test_a_changed_upshift_version_refuses_the_resume(tmp_path: Path, monkeypatch) -> None:
    """A run recorded by a different build of upshift is not the same evidence: the request
    translation, the check semantics and the recording format all live in that version."""
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    monkeypatch.setattr(recorder, "__version__", "99.0.0")
    with pytest.raises(ValueError, match="different evidence_id") as exc:
        _run(agent_dir, runs, _provider())
    assert "upshift_version" in str(exc.value)


def test_a_changed_model_refuses_the_resume(tmp_path: Path) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    with pytest.raises(ValueError, match="different evidence_id") as exc:
        run_suite(
            agent_dir,
            ScriptedProvider({"m2": {"base": set(CASES), "patched": set()}}),
            "tag",
            n_reps=2,
            model_override="m2",
            runs_root=runs,
            workers=1,
        )
    assert "model_requested" in str(exc.value)


def test_a_pre_v05_manifest_without_an_evidence_id_still_resumes(tmp_path: Path) -> None:
    """Backward compatibility is not a courtesy here: `runs/` in this repo is committed
    evidence, and a guard that made an old run unreadable would destroy it to protect it."""
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    manifest_path = runs / "tag" / "manifest.json"
    old = json.loads(manifest_path.read_text())
    del old["evidence_id"]
    manifest_path.write_text(json.dumps(old, indent=1, sort_keys=True))
    provider = _provider()
    _run(agent_dir, runs, provider)
    assert provider.requests == []


# ---------------------------------------------------------------------------
# Recomputation (what `upshift report` uses to say STALE)
# ---------------------------------------------------------------------------


def test_recompute_matches_while_the_files_are_untouched(tmp_path: Path) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    manifest = _manifest(runs)
    assert recorder.recompute_evidence_id(manifest) == manifest["evidence_id"]


def test_recompute_diverges_once_a_file_changes(tmp_path: Path) -> None:
    agent_dir, runs = _agent(tmp_path), tmp_path / "runs"
    _run(agent_dir, runs, _provider())
    manifest = _manifest(runs)
    (agent_dir / "prompt.txt").write_text("something else\n")
    assert recorder.recompute_evidence_id(manifest) != manifest["evidence_id"]


def test_recompute_returns_none_rather_than_guessing(tmp_path: Path) -> None:
    """'We cannot check' and 'we checked and it is unchanged' are different statements."""
    assert recorder.recompute_evidence_id({}) is None
    assert recorder.recompute_evidence_id({"agent_dir": str(tmp_path / "gone")}) is None


def test_the_real_sim_runner_records_an_evidence_id_too(tmp_path: Path) -> None:
    """Not only the scripted provider: the whole runner path carries §B."""
    agent_dir = Path(__file__).resolve().parent / "todo_agent"
    runs = tmp_path / "runs"
    run_suite(
        agent_dir, SimProvider(), "sim", n_reps=1, model_override="sim-5.5",
        runs_root=runs, workers=1,
    )
    manifest = json.loads((runs / "sim" / "manifest.json").read_text())
    assert len(manifest["evidence_id"]) == 64
