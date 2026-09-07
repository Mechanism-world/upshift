"""upshift CLI.

  upshift init     — scaffold an agent directory from the packaged example
  upshift capture  — record a framework agent's real /v1/messages requests at the wire
  upshift adapt    — generate an agent directory from an agent codebase, or from a capture
  upshift run      — execute the eval suite against one model/config, record everything
  upshift diff     — compare two recorded runs, print the behavioral diff report
  upshift upgrade  — full pipeline: baseline run, candidate run, diff, repair, verdict, patch
  upshift report   — re-render a saved diff/verdict
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import tempfile
from importlib import resources
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from upshift import recorder, verify_patch
from upshift.budget import (
    CostCeiling,
    CostCeilingExceeded,
    clear_stopped_marker,
    startup_warning,
    write_stopped_marker,
)
from upshift.capture import mapping as capture_mapping
from upshift.capture import record as capture_record
from upshift.capture import server as capture_server
from upshift.differ import diff_runs, load_diff, save_diff
from upshift.differ import passing_cases as differ_passing_cases
from upshift.native import cli as native_cli
from upshift.native.protocol import RunnerSpec
from upshift.patch import make_patch
from upshift.providers import get_provider
from upshift.providers.base import ProviderAPIError
from upshift.repair.loop import repair
from upshift.report import diff_to_markdown, render_diff
from upshift.schemas import ENDPOINTS, LABEL_REGRESSED, Case, validate_turn_params
from upshift.verdict import (
    BASELINE_BROKEN,
    EXIT_INCONCLUSIVE,
    INCONCLUSIVE,
    REASON_DESCRIPTIONS,
    REASON_EMPTY_SUITE,
    SAFE_WITH_PATCH,
    decide,
)

console = Console()

# Directory the repo checkout ships its experiment agent in; still auto-detected so the
# commands recorded in CLAUDE.md / DESIGN.md keep working without --agent.
LEGACY_AGENT_DIR = Path("victim/booking_agent")

# The models the bundled simulator knows about (providers/sim.py): one OpenAI pair on
# chat_completions/responses, one Anthropic pair on messages.
SIM_BASELINE_MODEL = "sim-5.5"
SIM_CANDIDATE_MODEL = "sim-5.6-sol"
SIM_FABLE_BASELINE_MODEL = "sim-fable-5"
SIM_FABLE_CANDIDATE_MODEL = "sim-fable-5-1"
_SIM_MODEL_PREFIXES = ("sim-5.5", "sim-5.6", "sim-fable-5")

#: Doc links printed to users, who often installed via pipx and have no repo checkout.
ADAPTER_URL = "https://github.com/Mechanism-world/upshift/blob/main/ADAPTER.md"

PROVIDERS = ["openai", "anthropic", "sim"]
#: model-id prefix -> the only provider that serves it
_MODEL_PROVIDER_PREFIXES = {"gpt-": "openai", "claude-": "anthropic"}

AGENT_FILE_BLURBS = {
    "agent.json": "model, endpoint, params — the config a repair may patch",
    "system_prompt.txt": "system prompt (patchable)",
    "tools.json": "tool schemas sent to the model (patchable)",
    "backend.py": "executes the tool calls; swap in your own",
    "cases/cases.json": "the eval suite",
}


def _progress(record) -> None:
    status = "[green]pass[/green]" if record.passed else "[red]FAIL[/red]"
    console.print(f"  {record.case_id} rep {record.rep}: {status}", highlight=False)


def _add_common_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--agent",
        default=None,
        help="agent directory (default: auto-detected in the current directory; "
        "`upshift init <dir>` creates one)",
    )
    p.add_argument(
        "--provider", default="openai", choices=list(PROVIDERS),
        help="which API to call: openai, anthropic, or sim (the free, keyless, "
        "deterministic simulator). Default openai.",
    )
    tier = p.add_mutually_exclusive_group()
    tier.add_argument(
        "--batch",
        action="store_true",
        help="execute API calls through the OpenAI Batch API (50%% token cost, slower "
        "wall-clock; turn-wave scheduling). Only valid with --provider openai.",
    )
    tier.add_argument(
        "--flex",
        action="store_true",
        help="use flex service tier: 50%% token cost like batch, synchronous, and prompt "
        "caching stacks on top (usually the cheapest option for this workload). Only "
        "valid with --provider openai.",
    )
    p.add_argument("--n", type=int, default=5, help="reps per case (default 5)")
    p.add_argument(
        "--runs-root", default=recorder.DEFAULT_RUNS_ROOT,
        help=f"directory run records are written to and resumed from (default "
        f"{recorder.DEFAULT_RUNS_ROOT}); must be outside the agent directory",
    )
    p.add_argument(
        "--workers", type=int, default=4,
        help="reps executed concurrently (default 4)",
    )
    p.add_argument("--quiet", action="store_true", help="no per-rep progress lines")
    p.add_argument(
        "--max-cost-usd", type=float, default=None,
        help="spend ceiling for everything this command records. The priced cost of the "
        "records already on disk is checked before every rep (and, for `upgrade`, between "
        "phases and repair candidates); on reaching it the command stops before the next "
        "API call, leaves every record resumable, and exits "
        f"{EXIT_COST_STOPPED} without a verdict. Default: no ceiling.",
    )


#: Distinct from 1 (a STAY PINNED verdict) and 2 (a usage/API error): the pipeline never
#: finished, so neither a verdict nor its absence says anything about the upgrade.
EXIT_COST_STOPPED = 3


def _cost_ceiling(args, prefix: str, models: list[str], *, descendants: bool) -> CostCeiling | None:
    """Build the ceiling for this command, warning up front about models we cannot price."""
    limit = getattr(args, "max_cost_usd", None)
    if limit is None:
        return None
    if limit <= 0:
        raise ValueError(f"--max-cost-usd must be positive (got {limit})")
    warning = startup_warning(args.provider, models)
    if warning:
        console.print(f"[bold yellow]WARNING:[/bold yellow] {escape(warning)}", soft_wrap=True)
    ceiling = CostCeiling(
        runs_root=args.runs_root, prefix=prefix, limit_usd=limit, descendants=descendants
    )
    if ceiling.spent_usd:
        console.print(
            f"[dim]${ceiling.spent_usd:.4f} of the ${limit:.2f} ceiling is already recorded "
            f"on disk for this {'tag' if descendants else 'run'}[/dim]",
            highlight=False,
        )
    return ceiling


def _report_cost_stop(stop: CostCeilingExceeded, marker: Path) -> int:
    console.print("\n[red]stopped: cost ceiling reached[/red]")
    console.print(
        f"  phase        {escape(stop.phase)}\n"
        f"  spent        ${stop.spent_usd:.4f} (priced from the records on disk)\n"
        f"  ceiling      ${stop.limit_usd:.2f}",
        highlight=False,
    )
    if stop.unpriced_models:
        console.print(
            f"  [yellow]note[/yellow]  {escape(', '.join(stop.unpriced_models))} has no "
            f"published rate; its usage was charged at the highest rate upshift knows, so "
            f"the total above is an upper bound",
            highlight=False,
            soft_wrap=True,
        )
    console.print(
        f"  no verdict was produced — this pipeline did not finish ({escape(str(marker))})",
        highlight=False,
        soft_wrap=True,
    )
    console.print(
        "  every completed rep is on disk: rerun the same command with a higher "
        "--max-cost-usd to resume, or price what is there with `upshift cost`.",
        soft_wrap=True,
    )
    return EXIT_COST_STOPPED


def _make_provider(args):
    batch, flex = getattr(args, "batch", False), getattr(args, "flex", False)
    if (batch or flex) and args.provider != "openai":
        raise ValueError(
            f"--batch/--flex are only valid with --provider openai (got "
            f"--provider {args.provider}); Anthropic has no batch or flex tier here"
        )
    # `adapt` has no --provider flag and no simulator, so pointing it at --provider sim
    # would be advice the reader cannot follow.
    sim_hint = (
        ", or use --provider sim for a free, deterministic run"
        if getattr(args, "command", None) != "adapt"
        else " — `adapt` always calls a real model (there is no simulator for it)"
    )
    if args.provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        # Without this the run would "succeed" with every rep recording an auth error.
        raise ValueError(
            "OPENAI_API_KEY is not set — export it (upshift also reads a .env file in the "
            "current directory)" + sim_hint
        )
    if args.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError(
            "ANTHROPIC_API_KEY is not set — export it (upshift also reads a .env file in the "
            "current directory)" + sim_hint
        )
    if batch:
        return get_provider("openai-batch")
    if flex:
        return get_provider("openai-flex")
    return get_provider(args.provider)


# ---------------------------------------------------------------------------
# The packaged example agent (src/upshift/example_agent), source for `upshift init`
# ---------------------------------------------------------------------------


def example_agent_root():
    """Traversable for the packaged example agent directory."""
    root = resources.files("upshift") / "example_agent"
    if not root.is_dir():
        raise ValueError(
            "the packaged example agent is missing from this upshift installation "
            "(expected upshift/example_agent/); reinstall upshift"
        )
    return root


def example_agent_files() -> list[str]:
    """Relative posix paths of every file in the packaged example agent."""
    found: list[str] = []

    def walk(node, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            if child.name == "__pycache__":
                continue
            rel = prefix + child.name
            if child.is_dir():
                walk(child, rel + "/")
            else:
                found.append(rel)

    walk(example_agent_root(), "")
    return sorted(found)


def read_example_agent_file(rel: str) -> bytes:
    node = example_agent_root()
    for part in rel.split("/"):
        node = node / part
    return node.read_bytes()


# ---------------------------------------------------------------------------
# Agent directory resolution + validation (every failure here is a clean one-liner)
# ---------------------------------------------------------------------------


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: malformed JSON ({e.msg} at line {e.lineno})") from e
    except OSError as e:
        raise ValueError(f"{path}: cannot be read ({e.strerror})") from e


class EmptySuiteError(ValueError):
    """`cases/cases.json` parsed, and holds no cases.

    A ValueError like every other authoring error, so `upshift run` still exits 2 with the
    message below — but its own class, because `upshift upgrade` owes the user a VERDICT and
    DESIGN.md §D names this one: an empty suite measures nothing, which is INCONCLUSIVE
    (exit 3), not a SAFE upgrade and not merely a typo.
    """


def _validate_cases(agent_dir: Path) -> list:
    """The eval suite, or a ValueError naming the file and the problem. Shared by both kinds
    of agent directory: the suite is the one thing a native agent still owes upshift."""
    cases_path = agent_dir / "cases" / "cases.json"
    if not cases_path.is_file():
        raise ValueError(
            f"{cases_path} not found — an agent directory needs its eval suite at cases/cases.json"
        )
    try:
        cases = Case.load_all(cases_path)
    except TypeError as e:
        raise ValueError(
            f"{cases_path}: every case needs id, description, initial_state, user_messages "
            f"and checks ({e})"
        ) from e
    except (KeyError, ValueError) as e:
        raise ValueError(f"{cases_path}: malformed eval suite ({e})") from e
    if not cases:
        raise EmptySuiteError(
            f"{cases_path}: the eval suite is empty — there is nothing to measure, so no run "
            f"could say anything about either model"
        )
    return cases


def _validate_native_agent_dir(agent_dir: Path, config_path: Path, raw: dict) -> dict:
    """Preflight for an agent.json with a `runner` block (DESIGN.md §C).

    Only `name`, `model` and a valid runner block are required: the endpoint, the prompt, the
    tool schemas and backend.py belong to the application, which builds its own requests. The
    runner block is validated by its own parser so the message names the field.
    """
    for key in ("name", "model"):
        if key not in raw:
            raise ValueError(f"{config_path}: missing required key {key!r} (native runner agent)")
    RunnerSpec.from_dict(raw["runner"], str(config_path))
    _validate_cases(agent_dir)
    return raw


def validate_agent_dir(agent_dir: Path) -> dict:
    """Fail fast, before any model call, with a message that names the file and the problem.

    Returns the parsed agent.json.
    """
    if not agent_dir.is_dir():
        raise ValueError(f"{agent_dir} is not a directory")
    config_path = agent_dir / "agent.json"
    if not config_path.is_file():
        raise ValueError(
            f"{config_path} not found — {agent_dir} is not an upshift agent directory "
            f"(needs agent.json, system_prompt.txt, tools.json, backend.py, cases/cases.json; "
            f"the contract is at {ADAPTER_URL}). Run `upshift init <dir>` to create one."
        )
    raw = _read_json(config_path)
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: expected a JSON object")  # noqa: TRY004
    if raw.get("runner") is not None:
        # DESIGN.md §C: a native agent's prompt, tools and backend are the APPLICATION's, and
        # upshift never builds a request for it — only `cases/` and the runner block are used.
        # Demanding the five adapter files here would make the native path impossible to use.
        return _validate_native_agent_dir(agent_dir, config_path, raw)
    for key in ("name", "endpoint", "model", "system_prompt_file", "tools_file"):
        if key not in raw:
            raise ValueError(f"{config_path}: missing required key {key!r}")
    if raw["endpoint"] not in ENDPOINTS:
        raise ValueError(
            f"{config_path}: endpoint {raw['endpoint']!r} is not one of {list(ENDPOINTS)}"
        )
    for key in ("system_prompt_file", "tools_file"):
        if not (agent_dir / str(raw[key])).is_file():
            raise ValueError(
                f"{config_path}: {key} points at {raw[key]!r}, which does not exist in {agent_dir}"
            )
    validate_turn_params(raw.get("turn_params"), str(config_path))
    tools = _read_json(agent_dir / str(raw["tools_file"]))
    # An empty list is legal: a plain completion agent (one system prompt, no tool loop) is
    # still a plain API agent, and the loop already handles it — `_mark_last_tool_cacheable`
    # is a documented no-op on an empty list and `build_request` sends `tools: []`. Rejecting
    # it here only blocked adapters for real toolless agents (e.g. a shell harness that POSTs
    # to /v1/messages and prints the text). A non-list is still an authoring error.
    if not isinstance(tools, list):
        raise ValueError(  # noqa: TRY004 - authoring errors are ValueError throughout preflight
            f"{agent_dir / str(raw['tools_file'])}: expected a JSON list of tool schemas"
        )

    _validate_cases(agent_dir)

    from upshift.runner import load_backend_factory

    try:
        load_backend_factory(agent_dir)
    except ValueError:
        raise  # runner's own message already names the file and the missing piece
    except Exception as e:  # any import-time failure must still read as one line
        raise ValueError(
            f"{agent_dir / 'backend.py'}: failed to import ({type(e).__name__}: {e})"
        ) from e
    return raw


def _detect_agent_dir() -> Path:
    if (LEGACY_AGENT_DIR / "agent.json").is_file():
        return LEGACY_AGENT_DIR
    try:
        candidates = sorted(
            d for d in Path().iterdir() if d.is_dir() and (d / "agent.json").is_file()
        )
    except OSError:
        candidates = []
    if len(candidates) == 1:
        console.print(f"[dim]agent directory: {escape(str(candidates[0]))}[/dim]", highlight=False)
        return candidates[0]
    if len(candidates) > 1:
        listed = ", ".join(str(c) for c in candidates[:6])
        raise ValueError(f"several agent directories here ({listed}); pick one with --agent <dir>")
    raise ValueError(
        "no agent directory: pass --agent <dir>, or run `upshift init my-agent` to create one "
        "from the packaged example"
    )


def resolve_agent_dir(
    explicit: str | None, runs_root: str | Path | None = None
) -> tuple[Path, dict]:
    """(agent directory, parsed agent.json) — validated, or a clean ValueError."""
    if explicit is None:
        agent_dir = _detect_agent_dir()
    else:
        agent_dir = Path(explicit)
        if not agent_dir.exists():
            raise ValueError(
                f"agent directory {explicit!r} does not exist — "
                f"`upshift init {explicit}` creates one from the packaged example"
            )
    raw_config = validate_agent_dir(agent_dir)
    if runs_root is not None:
        runs = Path(runs_root).resolve()
        agent = agent_dir.resolve()
        if runs == agent or agent in runs.parents:
            raise ValueError(
                f"the runs directory ({runs}) sits inside the agent directory ({agent}); "
                f"upshift copies the agent directory during repair, so keep them apart "
                f"(pass --runs-root <dir> outside the agent directory)"
            )
    return agent_dir, raw_config


def _check_models(provider_name: str, models: list[str]) -> None:
    """Catch the provider/model mix-ups that would otherwise fail 190 times in a row."""
    for model in models:
        if not model:
            continue
        if provider_name == "sim" and not model.startswith(_SIM_MODEL_PREFIXES):
            raise ValueError(
                f"provider 'sim' only simulates {SIM_BASELINE_MODEL!r} (baseline) and "
                f"{SIM_CANDIDATE_MODEL!r} (candidate), plus {SIM_FABLE_BASELINE_MODEL!r} -> "
                f"{SIM_FABLE_CANDIDATE_MODEL!r} on the messages endpoint; got {model!r}. "
                f"Pass those model names, or use --provider openai/anthropic for a real model."
            )
        if provider_name != "sim" and model.startswith("sim-"):
            raise ValueError(
                f"model {model!r} exists only in the local simulator; add --provider sim"
            )
        for prefix, owner in _MODEL_PROVIDER_PREFIXES.items():
            if model.startswith(prefix) and provider_name not in ("sim", owner):
                raise ValueError(
                    f"model {model!r} is served by provider {owner!r}, not "
                    f"{provider_name!r}; use --provider {owner}"
                )


def _effort_levels(capabilities: dict | None) -> list[str]:
    """Effort levels out of a /v1/models capabilities block, tolerating shape drift."""
    effort = (capabilities or {}).get("effort")
    if isinstance(effort, dict):
        for key in ("levels", "values", "supported", "supported_values"):
            value = effort.get(key)
            if isinstance(value, list):
                return [str(v) for v in value]
        return []
    if isinstance(effort, list):
        return [str(v) for v in effort]
    return []


def anthropic_preflight(provider, models: list[str]) -> str:
    """Free GET /v1/models/{id} for each model before any paid call. Prints one line per
    model and returns a notes string for the run manifest."""
    if getattr(provider, "name", "") != "anthropic":
        return ""
    ids = [m for m in dict.fromkeys(models) if m]
    if not ids:
        return ""
    try:
        capabilities = provider.preflight_models(ids)
    except ProviderAPIError as exc:
        if exc.status_code == 404:
            raise ValueError(
                f"model id not found for this ANTHROPIC_API_KEY ({', '.join(ids)}): "
                f"{exc.message}"
            ) from exc
        if "anthropic-workspace-id" in (exc.message or ""):
            raise ValueError(
                "this ANTHROPIC_API_KEY is identity-linked and every request must name a "
                "workspace: set ANTHROPIC_WORKSPACE_ID=<workspace id from the Anthropic "
                f"console> (also read from .env). API said: {exc.message}"
            ) from exc
        raise
    notes = []
    for model_id, capability in capabilities.items():
        levels = _effort_levels(capability)
        detail = f"effort levels {levels[0]}…{levels[-1]}" if levels else "no effort levels"
        console.print(f"  {escape(model_id)}: ok, {escape(detail)}", highlight=False)
        notes.append(f"{model_id}: {','.join(levels) if levels else 'effort levels unreported'}")
    return "preflight " + "; ".join(notes)


def _with_notes(notes: str, extra: str) -> str:
    return f"{notes} · {extra}" if notes and extra else (notes or extra)


def _positive(value: int, flag: str) -> int:
    if value < 1:
        raise ValueError(f"{flag} must be at least 1 (got {value})")
    return value


def _patch_prefix(agent_dir: Path) -> str:
    """Path the emitted patch is rooted at, so `git apply` works from the repo root."""
    try:
        return str(agent_dir.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return agent_dir.name


def cmd_init(args) -> int:
    dest = Path(args.directory)
    if dest.exists():
        if not dest.is_dir():
            raise ValueError(f"{dest} already exists and is not a directory")
        if any(dest.iterdir()):
            raise ValueError(
                f"{dest}/ already exists and is not empty — nothing was written. "
                f"Pick a new directory name."
            )
    files = example_agent_files()
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_example_agent_file(rel))

    n_cases = len(json.loads(read_example_agent_file("cases/cases.json")))
    console.print(f"created [bold]{escape(str(dest))}/[/bold] from the packaged example agent:")
    listed = [rel for rel in AGENT_FILE_BLURBS if rel in files]
    listed += [rel for rel in files if rel not in AGENT_FILE_BLURBS]
    width = max(len(rel) for rel in listed)
    for rel in listed:
        blurb = AGENT_FILE_BLURBS.get(rel, "")
        if rel == "cases/cases.json":
            blurb = f"{n_cases} eval cases, deterministic checks"
        console.print(f"  {rel:<{width}}  [dim]{blurb}[/dim]", highlight=False)
    console.print("\nnext — a full behavioral diff with no API key, free and deterministic:")
    console.print(
        f"  [bold]upshift upgrade --agent {escape(str(dest))} --provider sim "
        f"--baseline-model {SIM_BASELINE_MODEL} --candidate-model {SIM_CANDIDATE_MODEL} "
        f"--tag demo[/bold]",
        highlight=False,
        soft_wrap=True,
    )
    console.print("\nthen the same thing against real models (needs OPENAI_API_KEY, costs money):")
    console.print(
        f"  [bold]upshift upgrade --agent {escape(str(dest))} --provider openai --flex "
        f"--baseline-model gpt-5.5 --candidate-model gpt-5.6-sol --tag real[/bold]",
        highlight=False,
        soft_wrap=True,
    )
    console.print(
        f"\n[dim]both write run records, the diff, the verdict and the patch under "
        f"{Path('runs').resolve()}/[/dim]",
        highlight=False,
        soft_wrap=True,
    )
    console.print(
        "[dim]to point upshift at your own agent, rebuild these five files for it — the "
        f"contract is at {ADAPTER_URL}, and `upshift adapt <repo> --out <dir>` drafts "
        f"them from an existing codebase.[/dim]",
        soft_wrap=True,
    )
    return 0


# ---------------------------------------------------------------------------
# capture: the wire recorder that sits in front of a framework agent
# ---------------------------------------------------------------------------


def cmd_capture(args) -> int:
    """Record what the framework actually sends, until Ctrl-C."""
    from upshift.capture.server import DEFAULT_LISTEN, run_capture

    out_dir = Path(args.out)
    if out_dir.exists() and not out_dir.is_dir():
        raise ValueError(f"{out_dir} already exists and is not a directory")
    if out_dir.is_dir() and (out_dir / "index.json").exists():
        raise ValueError(
            f"{out_dir}/ already holds a finished capture (index.json) — pick a new directory, "
            f"so two runs of two different agents never become one adapter"
        )
    if args.max_body_bytes < 1024:
        raise ValueError(f"--max-body-bytes must be at least 1024 (got {args.max_body_bytes})")

    def on_ready(host: str, port: int) -> None:
        base = f"http://{host}:{port}"
        target = "the bundled simulator ($0, no network)" if args.sim else args.upstream
        console.print(f"recording {escape(base)} -> {escape(target)}")
        console.print(
            f"\npoint your agent at it and run it exactly as usual:\n"
            f"  [bold]ANTHROPIC_BASE_URL={escape(base)}[/bold]\n"
            f"[dim]per-framework settings: README, \"Framework agents (capture mode)\"[/dim]",
            highlight=False,
            soft_wrap=True,
        )
        console.print(
            f"\nwriting to {escape(str(out_dir.resolve()))}/ · Ctrl-C to stop and write "
            f"index.json",
            highlight=False,
            soft_wrap=True,
        )

    def on_record(conversation: str, index: int, status, error: str) -> None:
        if args.quiet:
            return
        bad = status is None or status >= 400
        state = f"[red]{status or 'no response'}[/red]" if bad else f"[green]{status}[/green]"
        suffix = f" — {escape(error)}" if error else ""
        console.print(f"  {conversation} req {index}: {state}{suffix}", highlight=False)

    index_path = run_capture(
        out_dir,
        listen=args.listen or DEFAULT_LISTEN,
        upstream=args.upstream,
        allow_remote=args.allow_remote,
        framework=args.framework,
        sim=args.sim,
        sim_agent=args.sim_agent,
        max_body_bytes=args.max_body_bytes,
        on_ready=on_ready,
        on_record=on_record,
    )
    index = json.loads(index_path.read_text())
    console.print(
        f"\ncaptured [bold]{index['requests']}[/bold] request(s) in "
        f"[bold]{index['conversations']}[/bold] conversation(s) · "
        f"model(s) {escape(', '.join(index['models']) or 'none')} · "
        f"tool(s) {escape(', '.join(index['tools']) or 'none')}",
        highlight=False,
        soft_wrap=True,
    )
    framework = (index.get("framework") or {}).get("framework")
    if framework and framework != "unknown":
        console.print(f"framework: {escape(framework)}", highlight=False)
    for key, values in (index.get("params_seen") or {}).items():
        console.print(
            f"  [dim]{key}: {escape(json.dumps(values))}[/dim]", highlight=False, soft_wrap=True
        )
    if not index["requests"]:
        console.print(
            "[yellow]nothing was recorded — check that your framework really read the base URL "
            "(the README table names the setting for each one)[/yellow]",
            soft_wrap=True,
        )
        return 2
    console.print(
        f"\nnext: [bold]upshift adapt --from-capture {escape(str(out_dir))} --out my-agent[/bold]",
        highlight=False,
        soft_wrap=True,
    )
    return 0


ADAPT_DEFAULT_MODEL = "gpt-5.5"
ADAPT_DEFAULT_MAX_COST = 2.00


def _adapt_out_dir(raw: str) -> Path:
    out_dir = Path(raw)
    if out_dir.exists():
        if not out_dir.is_dir():
            raise ValueError(f"{out_dir} already exists and is not a directory")
        if any(out_dir.iterdir()):
            raise ValueError(
                f"{out_dir}/ already exists and is not empty — nothing was written. "
                f"Pick a new directory name."
            )
    return out_dir


def _print_extraction_rounds(extraction) -> None:
    """What the pointer-following round did, in one console line."""
    round2 = getattr(extraction, "round2", None)
    if round2 is None:
        return
    if round2.used:
        console.print(
            f"  round 2 followed {len(round2.followed)} of {len(round2.pointers)} pointer(s) "
            f"into unread source in {escape(', '.join(round2.files))} · "
            f"{len(round2.settled)} claim(s) settled",
            highlight=False,
            soft_wrap=True,
        )
    elif round2.aborted:
        console.print(
            f"[yellow]  round 2 stopped ({escape(round2.aborted)}) — round 1's extraction "
            f"is what was used[/yellow]",
            highlight=False,
            soft_wrap=True,
        )
    elif round2.ran:
        console.print(
            "[yellow]  round 2 never satisfied the schema — round 1's extraction is what "
            "was used[/yellow]",
            soft_wrap=True,
        )
    else:
        console.print(f"  [dim]one round: {escape(round2.skipped)}[/dim]", highlight=False,
                      soft_wrap=True)


def cmd_adapt_from_capture(args) -> int:
    """Build the agent directory out of recorded requests — no model call, no source code."""
    from upshift.capture.adapt import adapt_from_capture

    out_dir = _adapt_out_dir(args.out)
    result = adapt_from_capture(args.from_capture, out_dir)
    for rel in result.files:
        console.print(f"  wrote {escape(str((out_dir / rel).resolve()))}", highlight=False)
    console.print(
        f"\n{result.case_count} case(s) from {result.case_count} captured conversation(s) · "
        f"{len(result.tool_names)} tool(s) · model {escape(result.model)}"
        + (f" · framework {escape(result.framework)}" if result.framework else ""),
        highlight=False,
        soft_wrap=True,
    )
    for note in result.notes:
        style = "yellow" if note.startswith("CONFLICT:") else "dim"
        console.print(f"  [{style}]{escape(note)}[/{style}]", highlight=False, soft_wrap=True)
    if result.turn_params:
        console.print(
            f"  [cyan]per-turn params derived[/cyan]: {escape(json.dumps(result.turn_params))} "
            f"— applied by turn index, last entry repeating",
            highlight=False, soft_wrap=True,
        )
    try:
        validate_agent_dir(out_dir)
        console.print("[green]the generated directory satisfies the ADAPTER.md contract[/green]")
    except ValueError as exc:
        console.print(
            f"[yellow]incomplete agent directory:[/yellow] {escape(str(exc))}", highlight=False
        )
        return 2
    console.print(
        f"[dim]every deviation from the recorded bytes is listed in "
        f"{escape(str((out_dir / 'ADAPT_EDITS.md').resolve()))}[/dim]",
        highlight=False,
        soft_wrap=True,
    )
    if result.conflicts:
        console.print(
            f"[yellow]{len(result.conflicts)} value(s) in this capture disagree with each "
            f"other; the adapter had to choose, so the generated directory does not "
            f"reproduce every recorded request[/yellow]",
            highlight=False, soft_wrap=True,
        )
        if args.strict:
            console.print(
                "[red]--strict: refusing a capture the adapter could not reproduce[/red] — "
                "read the CONFLICTS section of ADAPT_EDITS.md, then either re-capture the "
                "one shape you mean to measure or drop --strict to accept the choices above.",
                highlight=False, soft_wrap=True,
            )
            return 3
    return 0


def cmd_adapt(args) -> int:
    """Read an agent codebase, emit an agent directory plus ADAPT_REPORT.md."""
    if args.from_capture:
        if args.source:
            raise ValueError(
                "pass either a source repo or --from-capture, not both: they are two different "
                "ways to build the same five files"
            )
        return cmd_adapt_from_capture(args)
    if args.strict:
        raise ValueError(
            "--strict applies to --from-capture only: it refuses a capture whose recorded "
            "requests disagree with each other. Reading a repo has its own honesty gate "
            "(the mechanical verbatim gate; see ADAPT_REPORT.md)"
        )
    if not args.source:
        raise ValueError(
            "adapt needs a source: a repo path/git URL to read, or --from-capture <dir> to "
            "build from requests recorded by `upshift capture`"
        )

    from upshift.adapt import AdaptAborted
    from upshift.adapt.extract import extract
    from upshift.adapt.generate import generate, slugify
    from upshift.adapt.inventory import resolve_source, take_inventory
    from upshift.adapt.record import RecordingExtractor, run_id_for
    from upshift.adapt.report import render_report, write_report
    from upshift.adapt.verify import verify

    out_dir = _adapt_out_dir(args.out)
    if args.max_cost_usd is not None and args.max_cost_usd <= 0:
        raise ValueError(f"--max-cost-usd must be positive (got {args.max_cost_usd})")
    _positive(args.max_evidence_tokens, "--max-evidence-tokens")
    _positive(args.max_files, "--max-files")
    _check_models("openai", [args.model])
    provider = _make_provider(args)

    # ignore_cleanup_errors: a shallow clone contains read-only git objects, and losing a
    # temp directory must never fail a run that already wrote its report.
    with tempfile.TemporaryDirectory(prefix="upshift-adapt-", ignore_cleanup_errors=True) as tmp:
        console.rule("[bold]1/4 inventory")
        repo = resolve_source(args.source, Path(tmp))
        inventory = take_inventory(
            repo, max_files=args.max_files, max_tokens=args.max_evidence_tokens
        )
        if not inventory.files:
            raise ValueError(
                f"no file in {repo.origin} carries an OpenAI/LiteLLM signal — upshift adapt "
                f"reads plain OpenAI tool-calling agents (ADAPTER.md); there is nothing here "
                f"to extract"
            )
        console.print(
            f"  {inventory.scanned_files} files scanned · {len(inventory.files)} ranked · "
            f"{len(inventory.call_sites)} call site(s) · ~{inventory.evidence_tokens:,} "
            f"evidence tokens",
            highlight=False,
        )
        for signal in inventory.files[:8]:
            console.print(
                f"  [dim]{escape(signal.path)}  {signal.score:g}  "
                f"{escape(', '.join(signal.reasons[:3]))}[/dim]",
                highlight=False,
            )

        run_id = run_id_for(slugify(out_dir.name, fallback="agent"))
        extractor = RecordingExtractor(
            provider,
            model=args.model,
            run_id=run_id,
            runs_root=args.runs_root,
            max_cost_usd=args.max_cost_usd,
            source=repo.origin,
            commit=repo.commit,
        )
        extractor.start(
            evidence_tokens=inventory.evidence_tokens, out_dir=str(out_dir.resolve())
        )

        console.rule(f"[bold]2/4 extraction ({args.model})")
        extraction = None
        aborted = None
        try:
            extraction = extract(
                inventory,
                call_model=extractor,
                model=args.model,
                agent_hint=args.agent_hint,
            )
        except AdaptAborted as exc:
            aborted = exc.message
        except ProviderAPIError as exc:
            aborted = f"the extraction call failed: {exc.message}"
        finally:
            extractor.finalize(extraction)

        verification = generation = None
        if extraction is not None:
            for attempt in extraction.attempts:
                state = "schema-valid" if attempt.ok else "rejected"
                console.print(f"  attempt {attempt.index}: {state}", highlight=False)
            _print_extraction_rounds(extraction)
            console.rule("[bold]3/4 verification gate")
            verification = verify(extraction.data, repo.root)
            console.print(
                f"  {verification.checked} citation(s) checked · "
                f"{verification.downgraded} claim(s) downgraded",
                highlight=False,
            )
            console.rule("[bold]4/4 generate")
            generation = generate(
                verification, out_dir, origin=repo.origin, commit=repo.commit
            )

        report_text = render_report(
            inventory=inventory,
            extraction=extraction,
            verification=verification,
            generation=generation,
            cost=extractor.cost_info(),
            out_dir=out_dir,
            aborted=aborted,
        )
        report_path = write_report(report_text, out_dir)

    if generation is None:
        console.print(f"[red]error:[/red] {escape(aborted or 'extraction produced nothing')}")
        console.print(
            f"partial report: {escape(str(report_path.resolve()))}", highlight=False
        )
        return 2

    for rel in generation.files:
        console.print(f"  wrote {escape(str((out_dir / rel).resolve()))}", highlight=False)
    for artifact, level in sorted(verification.confidence.items()):
        color = {"high": "green", "medium": "yellow", "low": "red"}[level]
        console.print(f"  {artifact:<20} [{color}]{level}[/{color}]", highlight=False)

    try:
        validate_agent_dir(out_dir)
        console.print("[green]the generated directory satisfies the ADAPTER.md contract[/green]")
    except ValueError as exc:
        console.print(
            f"[yellow]incomplete agent directory:[/yellow] {escape(str(exc))}", highlight=False
        )

    console.print(
        f"\n[bold]{len(generation.must_review)} line(s) need review[/bold] — "
        f"{escape(str(report_path.resolve()))}",
        highlight=False,
        soft_wrap=True,
    )
    if aborted:
        console.print(f"[yellow]{escape(aborted)}[/yellow]", highlight=False, soft_wrap=True)
        return 2
    if not extraction.ok:
        console.print(
            "[yellow]the extraction never satisfied the schema; everything above is "
            "salvage — treat it as low confidence[/yellow]",
            soft_wrap=True,
        )
    console.print(
        f"[dim]extraction recorded in {escape(str(Path(args.runs_root).resolve()))}/{run_id}/ · "
        f"price it with `upshift cost {escape(run_id)}`[/dim]",
        highlight=False,
        soft_wrap=True,
    )
    return 0


# ---------------------------------------------------------------------------
# The native runner (DESIGN.md §C): authorization, options, wire capture
# ---------------------------------------------------------------------------


def _native_config(agent_dir: Path):
    """The loaded AgentConfig, or None when this directory is not a native-runner agent."""
    from upshift.schemas import AgentConfig

    config = AgentConfig.load(agent_dir)
    return config if config.runner is not None else None


def _runner_gate(agent_dir: Path, args) -> int | None:
    """`None` when the run may proceed, else the exit code to return.

    Executing an application's own command is a different act from reading its files, so the
    gate is checked here, before any provider is contacted and before any run directory is
    written: without --allow-runner upshift prints the exact argv it would have run and stops.
    """
    from upshift.schemas import AgentConfig

    return native_cli.check_authorized(
        AgentConfig.load(agent_dir), args, provider=args.provider, printer=console.print
    )


@contextlib.contextmanager
def _runner_execution(agent_dir: Path, args, *, runs_root, wire_name: str):
    """Yield the `runner_options` / `capture_session` kwargs for `run_suite`.

    A no-op for an ordinary adapter agent (the options are still built, and still say "not
    allowed", which is what an adapter run wants). For a native agent with `--capture`, the
    `upshift capture` recorder is started on loopback and the child's provider base URL is
    pointed at it, so the requests the APPLICATION serialized are attached to each rep record.
    """
    native = _native_config(agent_dir)
    session = None
    with contextlib.ExitStack() as stack:
        if native is not None and getattr(args, "capture", None) is not None:
            from upshift.native.runner import CaptureSession

            out_dir = (
                Path(args.capture) if args.capture
                else Path(runs_root) / f"{wire_name}-wire"
            )
            session = stack.enter_context(
                CaptureSession(out_dir, provider=args.provider)
            )
            console.print(
                f"[dim]capturing the application's own requests to "
                f"{escape(str(out_dir))} (workers forced to 1)[/dim]",
                highlight=False,
            )
        options = native_cli.runner_options(
            args,
            provider=args.provider,
            capture_env=session.env() if session is not None else None,
        )
        yield {"runner_options": options, "capture_session": session}


def cmd_run(args) -> int:
    from upshift.runner import run_suite

    _positive(args.n, "--n")
    _positive(args.workers, "--workers")
    # A run id names a directory under the runs root; refuse a path before any work happens.
    recorder.safe_component(args.run_id, "run id")
    provider = _make_provider(args)
    agent_dir, raw_config = resolve_agent_dir(args.agent, args.runs_root)
    denied = _runner_gate(agent_dir, args)
    if denied is not None:
        return denied
    model = args.model or str(raw_config["model"])
    native = _native_config(agent_dir) is not None
    # A native run never calls upshift's provider: the application does, with its own client
    # and its own model string. Preflighting that string against the simulator's or a
    # provider's model list would reject a perfectly good native run (DESIGN.md §C).
    if not native:
        _check_models(args.provider, [model])
    notes = _with_notes(
        args.notes, "" if native else anthropic_preflight(provider, [model])
    )
    ceiling = _cost_ceiling(args, args.run_id, [model], descendants=False)
    if ceiling is not None:
        ceiling.set_phase(f"run {args.run_id}")
    try:
        with _runner_execution(
            agent_dir, args, runs_root=args.runs_root, wire_name=args.run_id
        ) as native_kwargs:
            run_directory = run_suite(
                agent_dir,
                provider,
                args.run_id,
                n_reps=args.n,
                model_override=args.model,
                endpoint_override=args.endpoint,
                runs_root=args.runs_root,
                case_ids=args.case or None,
                workers=args.workers,
                notes=notes,
                on_rep_done=None if args.quiet else _progress,
                cost_ceiling=ceiling,
                **native_kwargs,
            )
    except CostCeilingExceeded as stop:
        marker = write_stopped_marker(
            recorder.run_dir(args.runs_root, args.run_id), stop, extra={"run_id": args.run_id}
        )
        return _report_cost_stop(stop, marker)
    clear_stopped_marker(run_directory)
    summary = json.loads((run_directory / "summary.json").read_text())
    passes = sum(1 for s in summary.values() if s["n"] and s["passes"] / s["n"] >= 0.8)
    console.print(
        f"run [bold]{escape(args.run_id)}[/bold] complete: {passes}/{len(summary)} cases "
        f"at pass-rate ≥ 0.8 · records in {escape(str(run_directory.resolve()))}"
    )
    return 0


def cmd_diff(args) -> int:
    result = diff_runs(
        recorder.run_dir(args.runs_root, args.baseline),
        recorder.run_dir(args.runs_root, args.candidate),
    )
    render_diff(result, console=console)
    out = Path(args.runs_root) / "diffs"
    out.mkdir(parents=True, exist_ok=True)
    diff_path = out / f"{args.baseline}__{args.candidate}.json"
    save_diff(result, diff_path)
    (out / f"{args.baseline}__{args.candidate}.md").write_text(diff_to_markdown(result))
    console.print(f"\nsaved: {escape(str(diff_path.resolve()))}")
    return 0


def _empty_suite_verdict(args, empty: EmptySuiteError) -> int:
    """DESIGN.md §D: an empty suite is INCONCLUSIVE(empty_suite), with the artifact to prove
    it. Written before any model is contacted — nothing was run, and the verdict says so."""
    verdict = {
        "verdict": INCONCLUSIVE,
        "reasons": [REASON_EMPTY_SUITE],
        "inconclusive_reason": REASON_EMPTY_SUITE,
        "reason_details": {REASON_EMPTY_SUITE: {"detail": str(empty), "cases": []}},
        "baseline_model": args.baseline_model,
        "candidate_model": args.candidate_model,
        "cases_total": 0,
        "runs": [],
    }
    out_dir = recorder.run_dir(args.runs_root, args.tag)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verdict.json").write_text(json.dumps(verdict, indent=1, sort_keys=True))
    console.print(
        f"[bold white on yellow] {INCONCLUSIVE} [/bold white on yellow] "
        f"{REASON_EMPTY_SUITE}: {REASON_DESCRIPTIONS[REASON_EMPTY_SUITE]}",
        highlight=False, soft_wrap=True,
    )
    console.print(f"[dim]{escape(str(empty))}[/dim]", highlight=False, soft_wrap=True)
    console.print(
        f"nothing was run and no model was contacted; verdict in "
        f"{escape(str((out_dir / 'verdict.json').resolve()))}",
        highlight=False, soft_wrap=True,
    )
    return EXIT_INCONCLUSIVE


def cmd_upgrade(args) -> int:
    from upshift.runner import run_suite

    _positive(args.n, "--n")
    _positive(args.workers, "--workers")
    if args.budget < 1:
        raise ValueError(
            f"--budget must be at least 1 (got {args.budget}); pass --no-repair to skip the "
            f"repair loop entirely"
        )
    # Every run id in this pipeline is derived from --tag, and one of the directories it
    # names is rewritten by the repair loop: check it before the first run starts.
    recorder.safe_component(args.tag, "tag")
    provider = _make_provider(args)
    try:
        agent_dir, _ = resolve_agent_dir(args.agent, args.runs_root)
    except EmptySuiteError as empty:
        return _empty_suite_verdict(args, empty)
    denied = _runner_gate(agent_dir, args)
    if denied is not None:
        return denied
    # Only a `adapt --from-capture` directory has one; None everywhere else, and None means
    # the report says nothing about frameworks rather than guessing at one.
    framework = capture_mapping.framework_of(agent_dir)
    # See cmd_run: for a native agent the model strings are the APPLICATION's, not ours.
    native = _native_config(agent_dir) is not None
    if not native:
        _check_models(args.provider, [args.baseline_model, args.candidate_model])
    preflight = (
        "" if native
        else anthropic_preflight(provider, [args.baseline_model, args.candidate_model])
    )
    tag = args.tag
    runs_root = args.runs_root
    baseline_id = f"{tag}-baseline"
    candidate_id = f"{tag}-candidate"

    # One ceiling for the whole pipeline: every run id here is derived from --tag, so the
    # baseline, the candidate and every repair screen/verify/adjudication run count against
    # the same budget. It is re-checked between phases as well as before every rep, so a
    # phase that lands exactly on the line does not start the next one.
    ceiling = _cost_ceiling(
        args, tag, [args.baseline_model, args.candidate_model], descendants=True
    )
    out_dir = recorder.run_dir(runs_root, tag)

    def stopped(stop: CostCeilingExceeded) -> int:
        marker = write_stopped_marker(
            out_dir,
            stop,
            extra={"tag": tag, "baseline_run_id": baseline_id, "candidate_run_id": candidate_id},
        )
        return _report_cost_stop(stop, marker)

    try:
        with _runner_execution(
            agent_dir, args, runs_root=runs_root, wire_name=tag
        ) as native_kwargs:
            console.rule(f"[bold]1/4 baseline run: {args.baseline_model}")
            if ceiling is not None:
                ceiling.check("1/4 baseline run")
            run_suite(
                agent_dir, provider, baseline_id, n_reps=args.n,
                model_override=args.baseline_model, runs_root=runs_root, workers=args.workers,
                notes=_with_notes("upgrade pipeline baseline", preflight),
                on_rep_done=None if args.quiet else _progress,
                cost_ceiling=ceiling,
                **native_kwargs,
            )
            if differ_passing_cases(recorder.run_dir(runs_root, baseline_id)) == 0:
                # Said here, between the legs, because the candidate run is the one that costs
                # money and a comparison against a baseline that passed nothing cannot mean
                # anything.
                console.print(
                    f"\n[bold white on red] {BASELINE_BROKEN} [/bold white on red] the baseline "
                    f"model {escape(args.baseline_model)} passed 0 cases: this suite does not "
                    f"work on the model it is supposed to work on, so nothing measured against "
                    f"{escape(args.candidate_model)} will mean anything. The pipeline will report "
                    f"{BASELINE_BROKEN}, never SAFE. Stop it now (Ctrl-C; completed reps are "
                    f"kept) and fix the agent directory or the eval suite first.\n",
                    highlight=False, soft_wrap=True,
                )
            console.rule(f"[bold]2/4 candidate run: {args.candidate_model}")
            if ceiling is not None:
                ceiling.check("2/4 candidate run")
            run_suite(
                agent_dir, provider, candidate_id, n_reps=args.n,
                model_override=args.candidate_model, runs_root=runs_root, workers=args.workers,
                notes=_with_notes("upgrade pipeline candidate (unpatched)", preflight),
                on_rep_done=None if args.quiet else _progress,
                cost_ceiling=ceiling,
                **native_kwargs,
            )

            console.rule("[bold]3/4 behavioral diff")
            diff = diff_runs(recorder.run_dir(runs_root, baseline_id),
                             recorder.run_dir(runs_root, candidate_id))
            regressed = [c for c in diff.cases if c.label == LABEL_REGRESSED]

            repair_outcome = None
            patch_path = None
            if regressed and not args.no_repair:
                console.rule(f"[bold]4/4 repair loop ({len(regressed)} regressed cases)")
                if ceiling is not None:
                    ceiling.check("4/4 repair loop")
                work_dir = out_dir / "patched_agent"
                repair_outcome = repair(
                    final_verify=not args.no_final_verify,
                    original_agent_dir=agent_dir,
                    work_dir=work_dir,
                    provider=provider,
                    candidate_model=args.candidate_model,
                    baseline_diff=diff,
                    n_reps=args.n,
                    runs_root=runs_root,
                    run_prefix=tag,
                    budget=args.budget,
                    workers=args.workers,
                    cost_ceiling=ceiling,
                    **native_kwargs,
                )
    except CostCeilingExceeded as stop:
        return stopped(stop)

    if repair_outcome is not None:
        for line in repair_outcome.log:
            # Log lines carry [repair_type] tags and ['case', 'lists'] — rich would eat them.
            console.print(f"[dim]{escape(line)}[/dim]", highlight=False)
        if repair_outcome.accepted_patches:
            patch_text = make_patch(
                agent_dir,
                work_dir,
                rel_prefix=_patch_prefix(agent_dir),
                header=capture_mapping.patch_header(
                    framework,
                    [
                        {"id": patch.id, "repair_type": patch.repair_type}
                        for patch in repair_outcome.accepted_patches
                    ],
                ),
            )
            patch_file = recorder.run_dir(runs_root, tag) / "upgrade.patch"
            patch_file.parent.mkdir(parents=True, exist_ok=True)
            patch_file.write_text(patch_text)
            patch_path = str(patch_file)

    verdict = decide(
        diff, repair_outcome, patch_path, framework=framework, runs_root=runs_root
    )
    console.print()
    render_diff(diff, console=console, verdict=verdict)

    out_dir.mkdir(parents=True, exist_ok=True)
    # The pipeline finished, so a marker from an earlier cost-stopped attempt is stale.
    clear_stopped_marker(out_dir)
    save_diff(diff, out_dir / "diff.json")
    (out_dir / "verdict.json").write_text(json.dumps(verdict, indent=1, sort_keys=True))
    (out_dir / "REPORT.md").write_text(diff_to_markdown(diff, verdict=verdict))
    console.print(
        f"\nartifacts in {escape(str(out_dir.resolve()))}/ : diff.json · verdict.json · REPORT.md"
        + (" · upgrade.patch" if patch_path else ""),
        highlight=False,
        soft_wrap=True,
    )
    console.print(
        f"[dim]every run record (both models, every repair candidate) is under "
        f"{escape(str(Path(runs_root).resolve()))}/[/dim]",
        highlight=False,
        soft_wrap=True,
    )
    if verdict["verdict"] == SAFE_WITH_PATCH:
        console.print(f"apply the repair with: [bold]git apply {escape(str(patch_path))}[/bold]")
    if verdict["verdict"] == INCONCLUSIVE:
        return EXIT_INCONCLUSIVE
    return 0 if verdict["verdict"] in ("SAFE", SAFE_WITH_PATCH) else 1


def cmd_cost(args) -> int:
    from upshift.pricing import run_cost

    root = Path(args.runs_root)
    if not root.is_dir():
        raise ValueError(f"no runs directory at {root.resolve()} — nothing has been recorded yet")
    if args.run_ids:
        run_dirs = [root / rid for rid in args.run_ids]
        missing = [str(d) for d in run_dirs if not (d / "manifest.json").is_file()]
        if missing:
            raise ValueError(f"no run recorded at: {', '.join(missing)}")
    else:
        run_dirs = sorted(d for d in root.iterdir() if (d / "manifest.json").exists())
        if not run_dirs:
            raise ValueError(f"no runs recorded under {root.resolve()}")
    total_usd = 0.0
    total_in = total_out = 0
    any_unknown = False
    for run_directory in run_dirs:
        c = run_cost(run_directory)
        usd = "$0 (sim)" if c["provider"] == "sim" else (
            f"${c['usd']:.4f}" if c["usd"] is not None else "unknown rate"
        )
        written = c.get("cache_creation_input_tokens", 0)
        cache_note = f"cached {c['cached_input_tokens']:,}" + (
            f", wrote {written:,}" if written else ""
        )
        console.print(
            f"{c['run_id']:<28} {c['provider']:<13} {c['model']:<24} "
            f"in {c['input_tokens']:>9,} ({cache_note})  "
            f"out {c['output_tokens']:>8,}  {usd}",
            highlight=False,
            soft_wrap=True,
        )
        if c["provider"] != "sim":
            total_in += c["input_tokens"]
            total_out += c["output_tokens"]
            if c["usd"] is None:
                any_unknown = True
            else:
                total_usd += c["usd"]
    console.print(
        f"{'TOTAL (real API)':<28} {'':<13} {'':<24} in {total_in:>9,}  out {total_out:>8,}  "
        + (f"${total_usd:.4f}" + (" + unknown-rate runs" if any_unknown else "")),
        highlight=False,
        soft_wrap=True,
    )
    return 0


def cmd_report(args) -> int:
    if not Path(args.diff_json).is_file():
        raise ValueError(
            f"{args.diff_json} not found — pass the diff.json written by `upshift upgrade` "
            f"(runs/<tag>/diff.json) or by `upshift diff` (runs/diffs/<a>__<b>.json)"
        )
    result = load_diff(args.diff_json)
    verdict = None
    verdict_path = Path(args.diff_json).parent / "verdict.json"
    if verdict_path.exists():
        verdict = json.loads(verdict_path.read_text())
    render_diff(result, console=console, verdict=verdict)
    return 0


INTERRUPT_MESSAGE = (
    "\n[yellow]interrupted.[/yellow] Completed reps are already on disk — rerun the same "
    "command to resume; finished reps are skipped."
)


def _install_interrupt_handler() -> None:
    """Ctrl-C exits now.

    Without this the KeyboardInterrupt only surfaces once the worker pool has drained every
    already-queued rep — minutes of API calls the user just asked to stop. Every finished rep
    is already on disk (recorder writes atomically), so exiting hard loses nothing.
    """

    def handler(signum, frame) -> None:
        console.print(INTERRUPT_MESSAGE)
        console.file.flush()
        os._exit(130)

    try:
        signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread; main()'s except clause still covers it
        pass


def _load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins, never logged."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("upshift")
    except PackageNotFoundError:  # source tree without an install
        return "0.0.0+src"


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="upshift",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"upshift {_version()}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init", help="scaffold an agent directory from the packaged example"
    )
    p_init.add_argument("directory", help="directory to create (must not exist, or be empty)")
    p_init.set_defaults(func=cmd_init)

    p_capture = sub.add_parser(
        "capture",
        help="record a framework agent's real /v1/messages requests, to adapt them verbatim",
    )
    p_capture.add_argument(
        "--out", required=True, help="capture directory to write (created if absent)"
    )
    p_capture.add_argument(
        "--listen", default=capture_server.DEFAULT_LISTEN,
        help=f"host:port to bind (default {capture_server.DEFAULT_LISTEN}; loopback only "
        f"unless --allow-remote)",
    )
    p_capture.add_argument(
        "--upstream", default=capture_server.DEFAULT_UPSTREAM,
        help=f"API every request is forwarded to (default {capture_server.DEFAULT_UPSTREAM})",
    )
    p_capture.add_argument(
        "--framework", default=None,
        help="name the framework instead of detecting it from the request headers",
    )
    p_capture.add_argument(
        "--allow-remote", action="store_true",
        help="permit a non-loopback --listen (this recorder handles your API key: don't)",
    )
    p_capture.add_argument(
        "--sim", action="store_true",
        help="answer from the bundled simulator instead of forwarding: $0, no key, no network",
    )
    p_capture.add_argument(
        "--sim-agent", default=None,
        help="agent directory whose cases supply the sim oracle plans (needs --sim)",
    )
    p_capture.add_argument(
        "--max-body-bytes", type=int, default=capture_record.DEFAULT_MAX_BODY_BYTES,
        help=f"per-body size cap (default {capture_record.DEFAULT_MAX_BODY_BYTES})",
    )
    p_capture.add_argument("--quiet", action="store_true", help="no per-request progress lines")
    p_capture.set_defaults(func=cmd_capture)

    p_adapt = sub.add_parser(
        "adapt",
        help="generate an agent directory from an agent codebase, or from a capture directory",
    )
    p_adapt.add_argument(
        "source", nargs="?", default=None,
        help="path to an agent repo, or a git URL to clone (omit with --from-capture)",
    )
    p_adapt.add_argument(
        "--from-capture", default=None,
        help="build the agent directory from an `upshift capture` directory instead of source "
        "code: the recorded requests are the agent, verbatim",
    )
    p_adapt.add_argument(
        "--out", required=True, help="directory to write (must not exist, or be empty)"
    )
    p_adapt.add_argument(
        "--strict", action="store_true",
        help="--from-capture only: exit non-zero when the recorded requests disagree with "
        "each other, so the generated directory cannot reproduce the capture",
    )
    p_adapt.add_argument(
        "--model", default=ADAPT_DEFAULT_MODEL,
        help=f"extraction model (default {ADAPT_DEFAULT_MODEL})",
    )
    p_adapt.add_argument(
        "--flex", action="store_true", help="use the flex service tier for the extraction call"
    )
    p_adapt.add_argument(
        "--agent-hint", default=None,
        help="free text about the agent (entry point, which mode to extract, ...)",
    )
    p_adapt.add_argument(
        "--max-cost-usd", type=float, default=ADAPT_DEFAULT_MAX_COST,
        help=f"abort with a partial report rather than exceed this (default "
        f"{ADAPT_DEFAULT_MAX_COST:.2f})",
    )
    p_adapt.add_argument(
        "--max-files", type=int, default=15,
        help="how many of the highest-ranked files are sent as evidence (default 15)",
    )
    p_adapt.add_argument(
        "--max-evidence-tokens", type=int, default=120_000,
        help="hard cap on evidence sent to the model (estimated as len/4)",
    )
    p_adapt.add_argument(
        "--runs-root", default=recorder.DEFAULT_RUNS_ROOT,
        help=f"directory the extraction call is recorded in "
        f"(default {recorder.DEFAULT_RUNS_ROOT})",
    )
    p_adapt.set_defaults(func=cmd_adapt, provider="openai", batch=False)

    p_run = sub.add_parser("run", help="run the eval suite against one model/config")
    _add_common_run_args(p_run)
    p_run.add_argument(
        "--run-id", required=True,
        help="name for this run; it is the directory under --runs-root and what "
        "`upshift diff` and `upshift cost` take",
    )
    p_run.add_argument("--model", default=None, help="override agent.json model")
    p_run.add_argument(
        "--endpoint", default=None, choices=list(ENDPOINTS),
        help="override agent.json endpoint",
    )
    p_run.add_argument("--case", action="append", help="run only these case ids (repeatable)")
    p_run.add_argument("--notes", default="", help="free text stored in the run manifest")
    native_cli.add_runner_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_diff = sub.add_parser("diff", help="compare two recorded runs")
    p_diff.add_argument("baseline", help="run id of the baseline run")
    p_diff.add_argument("candidate", help="run id of the candidate run")
    p_diff.add_argument(
        "--runs-root", default=recorder.DEFAULT_RUNS_ROOT,
        help=f"directory both runs were recorded in "
        f"(default {recorder.DEFAULT_RUNS_ROOT})",
    )
    p_diff.set_defaults(func=cmd_diff)

    p_up = sub.add_parser("upgrade", help="full pipeline: run both models, diff, repair, verdict")
    _add_common_run_args(p_up)
    p_up.add_argument(
        "--baseline-model", required=True, help="model version you run in production today"
    )
    p_up.add_argument(
        "--candidate-model", required=True, help="model version you want to upgrade to"
    )
    p_up.add_argument("--tag", required=True, help="name for this upgrade experiment")
    p_up.add_argument("--budget", type=int, default=6, help="max repair candidates (default 6)")
    p_up.add_argument(
        "--no-repair", action="store_true",
        help="stop after the behavioral diff; do not try to repair the regressions",
    )
    p_up.add_argument(
        "--no-final-verify", action="store_true",
        help="skip the fresh final full-suite run after the last accepted repair; the verdict "
        "is then labelled selection_evidence_only (DESIGN.md v0.5 §D)",
    )
    native_cli.add_runner_args(p_up)
    p_up.set_defaults(func=cmd_upgrade)

    p_cost = sub.add_parser("cost", help="exact token cost of recorded runs")
    p_cost.add_argument("run_ids", nargs="*", help="run ids; default: every run on disk")
    p_cost.add_argument(
        "--runs-root", default=recorder.DEFAULT_RUNS_ROOT,
        help=f"directory the runs were recorded in "
        f"(default {recorder.DEFAULT_RUNS_ROOT})",
    )
    p_cost.set_defaults(func=cmd_cost)

    p_rep = sub.add_parser("report", help="re-render a saved diff")
    p_rep.add_argument(
        "diff_json",
        help="path to a saved diff: runs/<tag>/diff.json or runs/diffs/<a>__<b>.json",
    )
    p_rep.set_defaults(func=cmd_report)

    verify_patch.add_parser(sub)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        console.print(
            "\nstart here: [bold]upshift init my-agent[/bold] — scaffolds an example agent "
            "and prints the free, no-API-key demo command."
        )
        return 0
    previous_sigint = signal.getsignal(signal.SIGINT)
    _install_interrupt_handler()
    try:
        return args.func(args)
    except CostCeilingExceeded as e:
        # Safety net: both commands that can set a ceiling already report the stop with
        # their own artifacts. This keeps the exit code distinct if one ever escapes.
        console.print(f"[red]stopped:[/red] {escape(str(e))}")
        return EXIT_COST_STOPPED
    except ValueError as e:
        console.print(f"[red]error:[/red] {escape(str(e))}")
        return 2
    except ProviderAPIError as e:
        console.print(f"[red]api error:[/red] {escape(e.message)}")
        return 2
    except KeyboardInterrupt:
        console.print(INTERRUPT_MESSAGE)
        return 130
    except OSError as e:
        console.print(f"[red]error:[/red] {escape(str(e))}")
        return 2
    finally:
        try:
            signal.signal(signal.SIGINT, previous_sigint)
        except ValueError:
            pass


if __name__ == "__main__":
    sys.exit(main())
