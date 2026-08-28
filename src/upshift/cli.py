"""upshift CLI.

  upshift run      — execute the eval suite against one model/config, record everything
  upshift diff     — compare two recorded runs, print the behavioral diff report
  upshift upgrade  — full pipeline: baseline run, candidate run, diff, repair, verdict, patch
  upshift report   — re-render a saved diff/verdict
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rich.console import Console

from upshift import recorder
from upshift.differ import diff_runs, load_diff, save_diff
from upshift.patch import make_patch
from upshift.providers import get_provider
from upshift.repair.loop import repair
from upshift.report import diff_to_markdown, render_diff
from upshift.schemas import LABEL_REGRESSED
from upshift.verdict import SAFE_WITH_PATCH, decide

console = Console()


def _progress(record) -> None:
    status = "[green]pass[/green]" if record.passed else "[red]FAIL[/red]"
    console.print(f"  {record.case_id} rep {record.rep}: {status}", highlight=False)


def _add_common_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", default="victim/booking_agent", help="victim agent directory")
    p.add_argument("--provider", default="openai", choices=["openai", "sim"])
    p.add_argument("--n", type=int, default=5, help="reps per case (default 5)")
    p.add_argument("--runs-root", default=recorder.DEFAULT_RUNS_ROOT)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--quiet", action="store_true", help="no per-rep progress lines")


def cmd_run(args) -> int:
    from upshift.runner import run_suite

    provider = get_provider(args.provider)
    run_directory = run_suite(
        args.agent,
        provider,
        args.run_id,
        n_reps=args.n,
        model_override=args.model,
        endpoint_override=args.endpoint,
        runs_root=args.runs_root,
        case_ids=args.case or None,
        workers=args.workers,
        notes=args.notes,
        on_rep_done=None if args.quiet else _progress,
    )
    summary = json.loads((run_directory / "summary.json").read_text())
    passes = sum(1 for s in summary.values() if s["n"] and s["passes"] / s["n"] >= 0.8)
    console.print(
        f"run [bold]{args.run_id}[/bold] complete: {passes}/{len(summary)} cases at "
        f"pass-rate ≥ 0.8 · records in {run_directory}"
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
    console.print(f"\nsaved: {diff_path}")
    return 0


def cmd_upgrade(args) -> int:
    from upshift.runner import run_suite

    provider = get_provider(args.provider)
    tag = args.tag
    runs_root = args.runs_root
    baseline_id = f"{tag}-baseline"
    candidate_id = f"{tag}-candidate"

    console.rule(f"[bold]1/4 baseline run: {args.baseline_model}")
    run_suite(
        args.agent, provider, baseline_id, n_reps=args.n,
        model_override=args.baseline_model, runs_root=runs_root, workers=args.workers,
        notes="upgrade pipeline baseline",
        on_rep_done=None if args.quiet else _progress,
    )
    console.rule(f"[bold]2/4 candidate run: {args.candidate_model}")
    run_suite(
        args.agent, provider, candidate_id, n_reps=args.n,
        model_override=args.candidate_model, runs_root=runs_root, workers=args.workers,
        notes="upgrade pipeline candidate (unpatched)",
        on_rep_done=None if args.quiet else _progress,
    )

    console.rule("[bold]3/4 behavioral diff")
    diff = diff_runs(recorder.run_dir(runs_root, baseline_id),
                     recorder.run_dir(runs_root, candidate_id))
    regressed = [c for c in diff.cases if c.label == LABEL_REGRESSED]

    repair_outcome = None
    patch_path = None
    if regressed and not args.no_repair:
        console.rule(f"[bold]4/4 repair loop ({len(regressed)} regressed cases)")
        work_dir = recorder.run_dir(runs_root, tag) / "patched_agent"
        repair_outcome = repair(
            original_agent_dir=args.agent,
            work_dir=work_dir,
            provider=provider,
            candidate_model=args.candidate_model,
            baseline_diff=diff,
            n_reps=args.n,
            runs_root=runs_root,
            run_prefix=tag,
            budget=args.budget,
            workers=args.workers,
        )
        for line in repair_outcome.log:
            console.print(f"[dim]{line}[/dim]", highlight=False)
        if repair_outcome.accepted_patches:
            patch_text = make_patch(args.agent, work_dir, rel_prefix=str(args.agent).rstrip("/"))
            patch_file = recorder.run_dir(runs_root, tag) / "upgrade.patch"
            patch_file.parent.mkdir(parents=True, exist_ok=True)
            patch_file.write_text(patch_text)
            patch_path = str(patch_file)

    verdict = decide(diff, repair_outcome, patch_path)
    console.print()
    render_diff(diff, console=console, verdict=verdict)

    out_dir = recorder.run_dir(runs_root, tag)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_diff(diff, out_dir / "diff.json")
    (out_dir / "verdict.json").write_text(json.dumps(verdict, indent=1, sort_keys=True))
    (out_dir / "REPORT.md").write_text(diff_to_markdown(diff, verdict=verdict))
    console.print(f"\nartifacts: {out_dir}/diff.json · verdict.json · REPORT.md"
                  + (f" · {patch_path}" if patch_path else ""))
    if verdict["verdict"] == SAFE_WITH_PATCH:
        console.print(f"apply the repair with: [bold]git apply {patch_path}[/bold]")
    return 0 if verdict["verdict"] in ("SAFE", SAFE_WITH_PATCH) else 1


def cmd_report(args) -> int:
    result = load_diff(args.diff_json)
    verdict = None
    verdict_path = Path(args.diff_json).parent / "verdict.json"
    if verdict_path.exists():
        verdict = json.loads(verdict_path.read_text())
    render_diff(result, console=console, verdict=verdict)
    return 0


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


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="upshift", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the eval suite against one model/config")
    _add_common_run_args(p_run)
    p_run.add_argument("--run-id", required=True)
    p_run.add_argument("--model", default=None, help="override agent.json model")
    p_run.add_argument("--endpoint", default=None, choices=["chat_completions", "responses"])
    p_run.add_argument("--case", action="append", help="run only these case ids (repeatable)")
    p_run.add_argument("--notes", default="")
    p_run.set_defaults(func=cmd_run)

    p_diff = sub.add_parser("diff", help="compare two recorded runs")
    p_diff.add_argument("baseline")
    p_diff.add_argument("candidate")
    p_diff.add_argument("--runs-root", default=recorder.DEFAULT_RUNS_ROOT)
    p_diff.set_defaults(func=cmd_diff)

    p_up = sub.add_parser("upgrade", help="full pipeline: run both models, diff, repair, verdict")
    _add_common_run_args(p_up)
    p_up.add_argument("--baseline-model", required=True)
    p_up.add_argument("--candidate-model", required=True)
    p_up.add_argument("--tag", required=True, help="name for this upgrade experiment")
    p_up.add_argument("--budget", type=int, default=6, help="max repair candidates (default 6)")
    p_up.add_argument("--no-repair", action="store_true")
    p_up.set_defaults(func=cmd_upgrade)

    p_rep = sub.add_parser("report", help="re-render a saved diff")
    p_rep.add_argument("diff_json")
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        console.print(f"[red]error:[/red] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
