"""`upshift verify-patch` — prove the exported patch is the thing that was verified.

DESIGN.md §E. The repair loop verifies `patched_agent/`, a directory. The user applies
`upgrade.patch`, a diff. Nothing until now checked that applying the diff to the ORIGINAL
agent directory reproduces the configuration the loop actually ran, and every gap between
those two — a file the diff cannot carry, a hunk that applies with fuzz, a `patched_agent/`
that drifted after the run — turns a verified result into an unverified one.

What this command proves, exactly:

    applying <patch> to a clean copy of <agent dir> yields an agent configuration that
    rebuilds, for every case, the SAME first request that is recorded in <run>.

It sends nothing and costs nothing: the comparison is against requests already on disk. That
also bounds the claim — a first request matching is not an episode matching, and this command
says nothing about turns 2..n or about tool results. `--live` (a fresh N-rep run of the
patched copy) is future work and deliberately absent rather than stubbed, so that no reader
can mistake a free structural check for a paid behavioural one.

Exit codes: 0 verified, 2 any mismatch or any failure to apply.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from upshift import agent_loop, recorder
from upshift.patch import patchable_files
from upshift.schemas import AgentConfig, Case

EXIT_OK = 0
EXIT_MISMATCH = 2

#: Fields a provider or the request builder adds per call and which carry no configuration:
#: comparing them would report a mismatch for a cache key that is a hash of the very request
#: it annotates. Removed from BOTH sides before comparison (DESIGN.md §E).
VOLATILE_KEYS = ("prompt_cache_key", "cache_control", "seed")


class VerificationError(ValueError):
    """The patch could not be applied, or the run/agent inputs are unusable."""


# ---------------------------------------------------------------------------
# Applying the patch
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def strip_level(patch_text: str, agent_relative: list[str]) -> int:
    """The `-p` level that turns this patch's paths into paths inside the agent directory.

    The patch was written with paths rooted at the user's repo
    (`victim/booking_agent/agent.json`), so the level depends on how deep the agent dir sits
    in it. Derived from the patch itself rather than assumed: every `diff --git` path must
    end with one of the agent's own relative file names, and all of them must agree on how
    many leading components to drop, or the patch is not a patch of this agent.
    """
    levels = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        target = line.split()[-1]  # "b/victim/booking_agent/agent.json"
        parts = target.split("/")
        match = None
        for rel in agent_relative:
            tail = rel.split("/")
            if len(tail) <= len(parts) and parts[-len(tail):] == tail:
                match = len(parts) - len(tail)
                break
        if match is None:
            raise VerificationError(
                f"patch touches {target!r}, which is not one of this agent's patchable files "
                f"({', '.join(agent_relative)}); refusing to apply it"
            )
        levels.add(match)
    if not levels:
        raise VerificationError("the patch contains no `diff --git` headers")
    if len(levels) > 1:
        raise VerificationError(
            f"the patch's file paths disagree on their root ({sorted(levels)}); it was not "
            f"produced for this agent directory"
        )
    return levels.pop()


def apply_patch(agent_dir: Path, patch_path: Path, dest: Path) -> dict[str, Any]:
    """Clean copy of ``agent_dir`` at ``dest`` with ``patch_path`` applied. Returns the
    commands run, so the report can state what was done rather than assert that it worked."""
    shutil.copytree(
        agent_dir, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
    )
    patch_text = patch_path.read_text()
    level = strip_level(patch_text, patchable_files(agent_dir))
    commands: list[str] = []

    # The agent dir is usually NOT a git repo (it is a directory in one, or nowhere at all),
    # so the copy gets a throwaway repo purely to make `git apply` available with its exact
    # `--check` semantics. Nothing is committed and the repo dies with the temp directory.
    init = _run_git(["init", "--quiet"], dest)
    commands.append("git init")
    if init.returncode != 0:
        raise VerificationError(f"could not initialise a temp git repo: {init.stderr.strip()}")

    # `--unsafe-paths` because the patch is applied from the copy's root and git otherwise
    # refuses paths it considers outside the work tree; the `-p` level derived above already
    # lands every path inside the copy, and `strip_level` refused any path that is not one of
    # this agent's own files, so nothing can escape it.
    args = ["apply", f"-p{level}", "--unsafe-paths"]
    check = _run_git([*args, "--check", str(patch_path)], dest)
    commands.append(f"git apply -p{level} --check {patch_path}")
    if check.returncode != 0:
        raise VerificationError(
            f"the exported patch does not apply cleanly to a clean copy of {agent_dir}: "
            f"{check.stderr.strip()}"
        )
    applied = _run_git([*args, str(patch_path)], dest)
    commands.append(f"git apply -p{level} {patch_path}")
    if applied.returncode != 0:
        raise VerificationError(f"git apply failed after --check passed: {applied.stderr.strip()}")
    return {"commands": commands, "strip_level": level}


# ---------------------------------------------------------------------------
# Comparing requests
# ---------------------------------------------------------------------------


def strip_volatile(value: Any) -> Any:
    """``value`` with every volatile key removed, recursively."""
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def diff_paths(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Dotted field paths where two request bodies disagree. Empty means identical."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out = []
        for key in sorted(set(expected) | set(actual)):
            here = f"{path}.{key}" if path else key
            if key not in expected:
                out.append(f"{here} (only in the rebuilt request)")
            elif key not in actual:
                out.append(f"{here} (only in the recorded request)")
            else:
                out += diff_paths(expected[key], actual[key], here)
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} recorded vs {len(actual)} rebuilt"]
        out = []
        for index, (a, b) in enumerate(zip(expected, actual, strict=True)):
            out += diff_paths(a, b, f"{path}[{index}]")
        return out
    if expected != actual:
        return [f"{path or '<root>'}: recorded {expected!r} != rebuilt {actual!r}"]
    return []


def first_recorded_request(run_directory: Path, case_id: str) -> dict[str, Any] | None:
    path = recorder.rep_path(run_directory, case_id, 1)
    if not path.is_file():
        return None
    calls = json.loads(path.read_text()).get("api_calls") or []
    return calls[0].get("request") if calls else None


def rebuild_first_request(
    config: AgentConfig, case: Case, model: str, endpoint: str, params: dict[str, Any]
) -> dict[str, Any]:
    """The first request the patched configuration would send for ``case``.

    Built the way `agent_loop.run_episode` builds it — the system prompt is a conversation
    item everywhere except `messages`, where it is a top-level field — so a divergence here
    is a divergence in the configuration, not in this function's idea of a request.
    """
    items: list[dict[str, Any]] = (
        []
        if endpoint == "messages"
        else [{"role": "system", "content": config.system_prompt}]
    )
    segments = list(case.user_messages or [])
    if not segments:
        raise VerificationError(f"case {case.id!r} has no user messages; nothing to rebuild")
    items.append({"role": "user", "content": segments[0]})
    turn_params = agent_loop.params_for_turn(params, list(config.turn_params or []), 0)
    return agent_loop.build_request(
        endpoint,
        model,
        turn_params,
        config.tools,
        items,
        system=config.system_prompt,
        volatile_suffix=config.volatile_suffix,
    )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def verify_patch(
    agent_dir: str | Path,
    patch_path: str | Path,
    run_directory: str | Path,
    *,
    verdict_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the §E ``patch_verification`` block. Never raises on a mismatch — a mismatch is
    a RESULT, recorded in the block — but raises ``VerificationError`` when the patch cannot
    be applied at all, because then there is nothing to compare."""
    agent_dir, patch_path = Path(agent_dir), Path(patch_path)
    run_directory = Path(run_directory)
    if not (agent_dir / "agent.json").is_file():
        raise VerificationError(f"{agent_dir} is not an agent directory (no agent.json)")
    if not patch_path.is_file():
        raise VerificationError(f"patch {patch_path} not found")
    manifest_path = run_directory / "manifest.json"
    if not manifest_path.is_file():
        raise VerificationError(
            f"{run_directory} is not a run directory (no manifest.json); pass the run that "
            f"VERIFIED the patch, e.g. runs/<tag>-c01-<hash>-verify"
        )
    manifest = json.loads(manifest_path.read_text())
    agent = manifest.get("agent") or {}
    patch_bytes = patch_path.read_bytes()

    block: dict[str, Any] = {
        "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "applies_cleanly": False,
        "scope": manifest.get("scope") or recorder.SCOPE_ADAPTED_AGENT,
        "run": manifest.get("run_id"),
        "evidence_ids": [manifest["evidence_id"]] if manifest.get("evidence_id") else [],
        "config": {
            "model_requested": agent.get("model_requested"),
            "endpoint": agent.get("endpoint"),
            "params": agent.get("params") or {},
            "n_reps": manifest.get("n_reps"),
        },
        "commands": [],
        "agent_files_sha256": {},
        "cases_checked": 0,
        "cases_without_a_recorded_request": [],
        "mismatches": [],
        "live": False,
    }

    with tempfile.TemporaryDirectory(prefix="upshift-verify-patch-") as tmp:
        dest = Path(tmp) / "agent"
        applied = apply_patch(agent_dir, patch_path, dest)
        block["applies_cleanly"] = True
        block["commands"] = applied["commands"]
        config = AgentConfig.load(dest)
        block["agent_files_sha256"] = config.file_hashes()

        model = agent.get("model_requested") or config.model
        endpoint = agent.get("endpoint") or config.endpoint
        params = agent.get("params")
        params = dict(params) if isinstance(params, dict) else dict(config.params)

        cases = Case.load_all(dest / "cases" / "cases.json")
        for case in cases:
            recorded = first_recorded_request(run_directory, case.id)
            if recorded is None:
                block["cases_without_a_recorded_request"].append(case.id)
                continue
            rebuilt = rebuild_first_request(config, case, model, endpoint, params)
            paths = diff_paths(strip_volatile(recorded), strip_volatile(rebuilt))
            block["cases_checked"] += 1
            if paths:
                block["mismatches"].append({"case_id": case.id, "fields": paths})

    block["verified"] = bool(
        block["applies_cleanly"] and block["cases_checked"] and not block["mismatches"]
    )

    target = Path(verdict_path) if verdict_path else run_directory.parent / "verdict.json"
    block["verdict_path"] = str(target) if target.is_file() else None
    if target.is_file():
        verdict = json.loads(target.read_text())
        verdict["patch_verification"] = block
        target.write_text(json.dumps(verdict, indent=1, sort_keys=True))
    return block


def _print(block: dict[str, Any]) -> None:
    print("patch verification")
    print(f"  patch sha256    : {block['patch_sha256']}")
    print(f"  applies cleanly : {block['applies_cleanly']}")
    print(f"  scope           : {block['scope']}")
    print(f"  run             : {block['run']}")
    print(f"  config          : {json.dumps(block['config'], sort_keys=True)}")
    for command in block["commands"]:
        print(f"  ran             : {command}")
    print(f"  cases compared  : {block['cases_checked']}")
    missing = block["cases_without_a_recorded_request"]
    if missing:
        print(f"  no rep_01 for   : {', '.join(missing)}")
    if block["mismatches"]:
        print("  MISMATCH: the patched agent does not rebuild the requests this run recorded.")
        for entry in block["mismatches"]:
            for field in entry["fields"]:
                print(f"    {entry['case_id']}: {field}")
    else:
        print("  every compared case rebuilds the recorded first request byte for byte")
    if block["verdict_path"]:
        print(f"  written to      : {block['verdict_path']}")
    print(
        "  note: this compares FIRST requests only, and sends nothing — it proves the "
        "exported patch is the configuration that was run, not that a fresh run would pass."
    )


def cmd_verify_patch(args) -> int:
    block = verify_patch(
        args.agent,
        args.patch,
        args.run,
        verdict_path=getattr(args, "verdict", None),
    )
    _print(block)
    if not block["verified"]:
        return EXIT_MISMATCH
    return EXIT_OK


def add_parser(sub) -> Any:
    parser = sub.add_parser(
        "verify-patch",
        help="prove the exported patch reproduces the configuration a run verified",
    )
    parser.add_argument("--agent", required=True, help="the ORIGINAL agent directory")
    parser.add_argument("--patch", required=True, help="the exported upgrade.patch")
    parser.add_argument(
        "--run",
        required=True,
        help="the run directory that verified the patch (runs/<tag>-cNN-<hash>-verify)",
    )
    parser.add_argument(
        "--verdict",
        default=None,
        help="verdict.json to write the patch_verification block into "
        "(default: <run>/../verdict.json when it exists)",
    )
    parser.set_defaults(func=cmd_verify_patch)
    return parser
