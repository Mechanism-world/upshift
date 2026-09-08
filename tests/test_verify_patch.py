"""DESIGN.md §E — `upshift verify-patch`: the exported patch IS what was verified.

Two artefacts came out of every SAFE WITH PATCH run and nothing compared them: the loop ran
`patched_agent/`, a directory; the user applies `upgrade.patch`, a diff. Every difference
between them — a file the diff cannot carry, a mode change, a missing trailing newline, a
hunk that no longer applies — silently downgrades a verified result to an unverified one.

This command re-applies the exported patch to a clean copy of the ORIGINAL agent dir and
proves the result rebuilds the exact first request the verifying run recorded. It sends
nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _verdict_support import PATCH_MARKER, ScriptedProvider, write_agent

from upshift import verify_patch as VP
from upshift.patch import PatchExportError, make_patch
from upshift.runner import run_suite

CASES = ["c1", "c2"]
MODEL = "cand-model"


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="verify-patch shells out to git apply")


@pytest.fixture
def workspace(tmp_path: Path):
    """An original agent, a patched copy, an exported patch, and the run that verified it."""
    original = write_agent(tmp_path / "agent", CASES)
    patched = write_agent(tmp_path / "patched_agent", CASES, prompt=f"BASE {PATCH_MARKER}")
    runs = tmp_path / "runs"
    provider = ScriptedProvider({MODEL: {"base": set(), "patched": set(CASES)}})
    run_suite(
        patched, provider, "t-c01-verify", n_reps=1, model_override=MODEL,
        runs_root=runs, workers=1,
    )
    patch_file = tmp_path / "upgrade.patch"
    patch_file.write_text(make_patch(original, patched, rel_prefix="victim/agent"))
    return {
        "original": original,
        "patched": patched,
        "patch": patch_file,
        "run": runs / "t-c01-verify",
        "runs": runs,
        "tmp": tmp_path,
    }


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_faithful_patch_verifies(workspace) -> None:
    block = VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])
    assert block["applies_cleanly"] is True
    assert block["mismatches"] == []
    assert block["cases_checked"] == len(CASES)
    assert block["verified"] is True


def test_the_block_carries_everything_e_requires(workspace) -> None:
    block = VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])
    for key in (
        "patch_sha256", "applies_cleanly", "scope", "agent_files_sha256",
        "recorded_config", "patched_config", "config_mismatches", "reason",
        "commands", "mismatches", "evidence_ids", "live",
    ):
        assert key in block, key
    assert len(block["patch_sha256"]) == 64
    assert block["recorded_config"]["model_requested"] == MODEL
    assert block["recorded_config"]["endpoint"] == "chat_completions"
    assert block["patched_config"]["endpoint"] == "chat_completions"
    assert block["scope"] == "adapted_agent"
    assert block["live"] is False
    assert any("git apply" in c and "--check" in c for c in block["commands"])


def test_the_exit_code_is_zero_and_the_block_is_printed(workspace, capsys) -> None:
    args = _args(workspace)
    assert VP.cmd_verify_patch(args) == VP.EXIT_OK
    out = capsys.readouterr().out
    assert "patch verification" in out
    assert "rebuilds the recorded first request" in out


def test_it_is_written_into_verdict_json_next_to_the_run(workspace) -> None:
    verdict_path = workspace["runs"] / "verdict.json"
    verdict_path.write_text(json.dumps({"verdict": "SAFE WITH PATCH"}))
    VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])
    written = json.loads(verdict_path.read_text())
    assert written["patch_verification"]["verified"] is True
    assert written["verdict"] == "SAFE WITH PATCH"


def test_it_works_when_the_agent_dir_is_not_a_git_repo(workspace) -> None:
    """The common case: an agent directory is a folder, not a repository."""
    assert not (workspace["original"] / ".git").exists()
    assert VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])["verified"]


# ---------------------------------------------------------------------------
# The failure this command exists to catch
# ---------------------------------------------------------------------------


def test_a_patch_that_does_not_match_the_run_reports_the_field(workspace, capsys) -> None:
    """The divergence `patched_agent/` vs `upgrade.patch` was invisible before: here the
    exported patch writes a DIFFERENT prompt from the one the run actually sent."""
    text = workspace["patch"].read_text().replace(PATCH_MARKER, "SOMETHING-ELSE")
    workspace["patch"].write_text(text)
    block = VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])
    assert block["verified"] is False
    assert block["mismatches"]
    fields = block["mismatches"][0]["fields"]
    assert any("messages[0].content" in f for f in fields), fields
    assert VP.cmd_verify_patch(_args(workspace)) == VP.EXIT_MISMATCH
    assert "MISMATCH" in capsys.readouterr().out


def test_a_patch_that_does_not_apply_is_an_error(workspace) -> None:
    (workspace["original"] / "prompt.txt").write_text("a completely different prompt\n")
    with pytest.raises(VP.VerificationError, match="does not apply cleanly"):
        VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])


def test_a_patch_for_another_agent_is_refused(workspace, tmp_path: Path) -> None:
    workspace["patch"].write_text(
        "diff --git a/other/thing.py b/other/thing.py\n"
        "--- a/other/thing.py\n+++ b/other/thing.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    with pytest.raises(VP.VerificationError, match="not one of this agent's patchable files"):
        VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])


def test_a_run_dir_without_a_manifest_is_an_error(workspace, tmp_path: Path) -> None:
    with pytest.raises(VP.VerificationError, match="not a run directory"):
        VP.verify_patch(workspace["original"], workspace["patch"], tmp_path / "nope")


# ---------------------------------------------------------------------------
# Volatile fields, and the strip level
# ---------------------------------------------------------------------------


def test_volatile_fields_are_ignored_on_both_sides() -> None:
    """A cache key is a hash of the request it annotates; comparing it would report a
    mismatch for a field whose only content is the rest of the message."""
    recorded = {"model": "m", "prompt_cache_key": "abc", "system": [{"text": "x",
                "cache_control": {"type": "ephemeral"}}], "seed": 1}
    rebuilt = {"model": "m", "system": [{"text": "x"}]}
    assert VP.diff_paths(VP.strip_volatile(recorded), VP.strip_volatile(rebuilt)) == []


def test_diff_paths_names_the_differing_field() -> None:
    assert VP.diff_paths({"a": {"b": 1}}, {"a": {"b": 2}}) == ["a.b: recorded 1 != rebuilt 2"]
    assert VP.diff_paths({"a": [1, 2]}, {"a": [1]}) == ["a: length 2 recorded vs 1 rebuilt"]
    assert VP.diff_paths({"a": 1}, {}) == ["a (only in the recorded request)"]
    assert VP.diff_paths({}, {"a": 1}) == ["a (only in the rebuilt request)"]


@pytest.mark.parametrize("prefix", ["agent", "victim/agent", "a/b/c/agent"])
def test_the_strip_level_is_derived_from_the_patch(workspace, tmp_path: Path, prefix) -> None:
    """The patch's paths are rooted wherever the agent dir sits in the user's repo; the
    right `-p` level is read off the patch, never assumed."""
    patch_file = tmp_path / f"{prefix.replace('/', '_')}.patch"
    patch_file.write_text(make_patch(workspace["original"], workspace["patched"], prefix))
    assert VP.verify_patch(workspace["original"], patch_file, workspace["run"])["verified"]


# ---------------------------------------------------------------------------
# make_patch: the export must BE what was verified (fixed at the source)
# ---------------------------------------------------------------------------


def test_a_file_outside_the_patchable_three_refuses_export(workspace) -> None:
    """`git apply` would never reproduce it, so exporting a patch that omits it would hand
    the user a configuration that is not the one the loop ran."""
    (workspace["patched"] / "backend.py").write_text("# changed after the run\n")
    with pytest.raises(PatchExportError, match="backend.py: changed"):
        make_patch(workspace["original"], workspace["patched"], "victim/agent")


def test_an_added_file_refuses_export(workspace) -> None:
    (workspace["patched"] / "extra.json").write_text("{}\n")
    with pytest.raises(PatchExportError, match="extra.json: added"):
        make_patch(workspace["original"], workspace["patched"], "victim/agent")


def test_a_mode_change_refuses_export(workspace) -> None:
    """difflib emits content hunks only: an executable bit set in `patched_agent/` would
    never reach the user, and the patch would silently be a different thing."""
    (workspace["patched"] / "prompt.txt").chmod(0o755)
    with pytest.raises(PatchExportError, match="file mode"):
        make_patch(workspace["original"], workspace["patched"], "victim/agent")


def test_pycache_is_not_a_divergence(workspace) -> None:
    """The final verification run imports backend.py from the patched dir, which leaves a
    __pycache__ behind (this fixture already has one, from run_suite importing backend.py).
    That is a build dropping, not a repair."""
    (workspace["patched"] / "__pycache__").mkdir(exist_ok=True)
    (workspace["patched"] / "__pycache__" / "backend.pyc").write_bytes(b"\x00")
    assert make_patch(workspace["original"], workspace["patched"], "victim/agent")


def test_a_missing_trailing_newline_round_trips(tmp_path: Path) -> None:
    """difflib emits the last line without its newline and says nothing; git needs the
    `\\ No newline at end of file` marker or the patch adds a newline the run never had."""
    original = write_agent(tmp_path / "orig", CASES)
    patched = write_agent(tmp_path / "patched", CASES)
    (patched / "prompt.txt").write_text("BASE no trailing newline")  # no "\n"
    text = make_patch(original, patched, "agent")
    assert "\\ No newline at end of file" in text

    # And it applies, reproducing the byte-exact file.
    dest = tmp_path / "applied"
    shutil.copytree(original, dest)
    subprocess.run(["git", "init", "--quiet"], cwd=dest, check=True)
    patch_file = tmp_path / "p.patch"
    patch_file.write_text(text)
    subprocess.run(
        ["git", "apply", "-p2", "--unsafe-paths", str(patch_file)], cwd=dest, check=True
    )
    assert (dest / "prompt.txt").read_bytes() == (patched / "prompt.txt").read_bytes()


def _args(workspace):
    class Args:
        agent = str(workspace["original"])
        patch = str(workspace["patch"])
        run = str(workspace["run"])
        verdict = None

    return Args()


def test_add_parser_registers_the_subcommand() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    VP.add_parser(sub)
    args = parser.parse_args(["verify-patch", "--agent", "a", "--patch", "p", "--run", "r"])
    assert args.func is VP.cmd_verify_patch
    assert (args.agent, args.patch, args.run, args.verdict) == ("a", "p", "r", None)


# ---------------------------------------------------------------------------
# FINDING 1 — a case the run never recorded is not a verified case
# ---------------------------------------------------------------------------


def test_a_case_without_a_recorded_request_fails_verification(workspace, capsys) -> None:
    """`cases_without_a_recorded_request` was collected and then ignored: a run that skipped
    a case still reported verified=True over the cases it happened to have."""
    from upshift import recorder

    recorder.rep_path(workspace["run"], CASES[1], 1).unlink()
    block = VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])
    assert block["cases_without_a_recorded_request"] == [CASES[1]]
    assert block["cases_checked"] == 1
    assert block["mismatches"] == []
    assert block["verified"] is False
    assert "cases_without_a_recorded_request" in (block["reason"] or "")
    assert VP.cmd_verify_patch(_args(workspace)) == VP.EXIT_MISMATCH
    assert "cases_without_a_recorded_request" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# FINDING 2 — the rebuild must come from the PATCH, not from the run's manifest
# ---------------------------------------------------------------------------


def _half_patch(workspace, tmp_path: Path, **agent_json_edits):
    """A patch that carries the prompt change but NOT the agent.json change the run ran."""
    half = write_agent(tmp_path / "half", CASES, prompt=f"BASE {PATCH_MARKER}")
    patch_file = tmp_path / "half.patch"
    patch_file.write_text(make_patch(workspace["original"], half, rel_prefix="victim/agent"))
    return patch_file


@pytest.fixture
def param_workspace(tmp_path: Path):
    """The verified run used `temperature`; the exported patch lost it."""
    original = write_agent(tmp_path / "agent", CASES)
    patched = write_agent(tmp_path / "patched_agent", CASES, prompt=f"BASE {PATCH_MARKER}")
    raw = json.loads((patched / "agent.json").read_text())
    raw["params"] = {"temperature": 0.5}
    (patched / "agent.json").write_text(json.dumps(raw, indent=1))
    runs = tmp_path / "runs"
    provider = ScriptedProvider({MODEL: {"base": set(), "patched": set(CASES)}})
    run_suite(
        patched, provider, "t-c01-verify", n_reps=1, model_override=MODEL,
        runs_root=runs, workers=1,
    )
    return {
        "original": original,
        "patched": patched,
        "patch": None,
        "run": runs / "t-c01-verify",
        "runs": runs,
        "tmp": tmp_path,
    }


def test_a_patch_that_lost_the_param_change_does_not_verify(param_workspace, tmp_path) -> None:
    """The rebuild used to read `params` off the verifying run's manifest, so a patch that
    dropped the param change was checked against the param it dropped."""
    patch_file = _half_patch(param_workspace, tmp_path)
    block = VP.verify_patch(param_workspace["original"], patch_file, param_workspace["run"])
    assert block["patched_config"]["params"] == {}
    assert block["recorded_config"]["params"] == {"temperature": 0.5}
    assert block["config_mismatches"], block
    assert any(m["field"] == "params" for m in block["config_mismatches"])
    assert block["verified"] is False


@pytest.fixture
def endpoint_workspace(tmp_path: Path):
    """The verified run ran on `responses`; the exported patch lost the routing change."""
    original = write_agent(tmp_path / "agent", CASES)
    patched = write_agent(tmp_path / "patched_agent", CASES, prompt=f"BASE {PATCH_MARKER}")
    raw = json.loads((patched / "agent.json").read_text())
    raw["endpoint"] = "responses"
    (patched / "agent.json").write_text(json.dumps(raw, indent=1))
    runs = tmp_path / "runs"
    provider = ScriptedProvider({MODEL: {"base": set(), "patched": set(CASES)}})
    run_suite(
        patched, provider, "t-c01-verify", n_reps=1, model_override=MODEL,
        runs_root=runs, workers=1,
    )
    return {"original": original, "run": runs / "t-c01-verify"}


def test_a_patch_that_lost_the_endpoint_change_does_not_verify(
    endpoint_workspace, tmp_path
) -> None:
    patch_file = _half_patch(endpoint_workspace, tmp_path)
    block = VP.verify_patch(endpoint_workspace["original"], patch_file, endpoint_workspace["run"])
    assert block["patched_config"]["endpoint"] == "chat_completions"
    assert block["recorded_config"]["endpoint"] == "responses"
    assert any(m["field"] == "endpoint" for m in block["config_mismatches"])
    assert block["verified"] is False


def test_the_cli_candidate_model_is_not_a_config_mismatch(workspace) -> None:
    """`--candidate` is passed on the command line and no patch can carry it, so the model
    is rebuilt from the run and REPORTED as not proven, never silently compared."""
    block = VP.verify_patch(workspace["original"], workspace["patch"], workspace["run"])
    assert block["patched_config"]["model"] == "scripted-base"
    assert block["recorded_config"]["model_requested"] == MODEL
    assert block["config_mismatches"] == []
    assert block["fields_not_proven_by_the_patch"] == ["model"]
    assert block["verified"] is True
