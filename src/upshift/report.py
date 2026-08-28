"""The 30-second engineer report.

Two renderers over the same content: ``render_diff`` (rich, terminal) and
``diff_to_markdown`` (plain text, for pasting into a PR or an issue). The goal is that an
engineer finds WHAT regressed and HOW BAD in under thirty seconds, so stable-pass cases never
appear as rows — only as a count.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from upshift import stats
from upshift.differ import CaseDiff, DiffResult
from upshift.schemas import (
    LABEL_FLAKY,
    LABEL_IMPROVED,
    LABEL_REGRESSED,
    LABEL_STABLE_FAIL,
    LABEL_STABLE_PASS,
    OUTCOME_PASS,
)

SIM_WARNING = "SIMULATED PROVIDER - machinery validation only, not evidence about real models"

#: Order of the label-counts one-liner.
_COUNT_ORDER = (
    LABEL_STABLE_PASS,
    LABEL_REGRESSED,
    LABEL_FLAKY,
    LABEL_IMPROVED,
    LABEL_STABLE_FAIL,
)

#: Order of the detail table: regressed first, then flaky, improved, stable-fail.
_DETAIL_ORDER = (LABEL_REGRESSED, LABEL_FLAKY, LABEL_IMPROVED, LABEL_STABLE_FAIL)

_LABEL_COLOR = {
    LABEL_REGRESSED: "bold red",
    LABEL_IMPROVED: "green",
    LABEL_FLAKY: "yellow",
    LABEL_STABLE_FAIL: "dim",
    LABEL_STABLE_PASS: "dim green",
}

_VERDICT_COLOR = {"SAFE": "green", "SAFE WITH PATCH": "yellow", "STAY PINNED": "red"}

DETAIL_WIDTH = 60


# ---------------------------------------------------------------------------
# Content helpers (shared by both renderers)
# ---------------------------------------------------------------------------


def _agent(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest.get("agent") or {}


def _model_endpoint(manifest: dict[str, Any]) -> str:
    agent = _agent(manifest)
    model = agent.get("model_requested") or "?"
    endpoint = agent.get("endpoint") or "?"
    return f"{model} @ {endpoint}"


def _providers(result: DiffResult) -> tuple[str, bool]:
    """(display string, simulated?) — simulated when either run is not a real OpenAI
    provider ('openai' sync or 'openai-batch'; batching changes transport, not evidence)."""
    b = str(result.baseline_manifest.get("provider", "?"))
    c = str(result.candidate_manifest.get("provider", "?"))
    display = b if b == c else f"{b} -> {c}"
    real = ("openai", "openai-batch")
    return display, not (b in real and c in real)


def _n_reps(result: DiffResult) -> Any:
    return result.baseline_manifest.get("n_reps", "?")


def _thresholds(result: DiffResult) -> tuple[float, float]:
    t = result.baseline_manifest.get("thresholds") or {}
    return float(t.get("pass", 0.8)), float(t.get("fail", 0.4))


def _fmt_rate(passing: int, total: int) -> str:
    """e.g. "31/38 cases pass (81.6%, CI 66.6-90.8%)"."""
    lo, hi = stats.wilson_interval(passing, total)
    pct = (passing / total * 100) if total else 0.0
    return f"{passing}/{total} cases pass ({pct:.1f}%, CI {lo * 100:.1f}-{hi * 100:.1f}%)"


def _suite_rates(result: DiffResult) -> tuple[str, str]:
    total = len(result.cases)
    b = sum(1 for c in result.cases if c.baseline_outcome == OUTCOME_PASS)
    c_ = sum(1 for c in result.cases if c.candidate_outcome == OUTCOME_PASS)
    return _fmt_rate(b, total), _fmt_rate(c_, total)


def _fmt_p(p: float) -> str:
    """3 significant figures, with significance stars: ** at p<0.05, * at p<0.10."""
    stars = "**" if p < 0.05 else ("*" if p < 0.10 else "")
    return f"p={p:.3g}" + (f" {stars}" if stars else "")


def _label_counts_line(result: DiffResult) -> str:
    parts = [f"{result.counts[name]} {name}" for name in _COUNT_ORDER if result.counts.get(name)]
    extra = sorted(k for k in result.counts if k not in _COUNT_ORDER)
    parts += [f"{result.counts[k]} {k}" for k in extra]
    return " · ".join(parts) if parts else "no cases"


def _detail_cases(result: DiffResult) -> list[CaseDiff]:
    rank = {name: i for i, name in enumerate(_DETAIL_ORDER)}
    rows = [c for c in result.cases if c.label != LABEL_STABLE_PASS]
    return sorted(rows, key=lambda c: (rank.get(c.label, len(rank)), c.p_value, c.case_id))


def _truncate(text: str, width: int = DETAIL_WIDTH) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _row_cells(case: CaseDiff) -> tuple[str, str, str, str, str, str, str]:
    return (
        case.case_id,
        case.label,
        f"{case.baseline_passes}/{case.baseline_n}",
        f"{case.candidate_passes}/{case.candidate_n}",
        _fmt_p(case.p_value),
        ", ".join(case.failure_signatures),
        _truncate(case.failing_check_details[0] if case.failing_check_details else ""),
    )


def _footnote(result: DiffResult) -> list[str]:
    pass_t, fail_t = _thresholds(result)
    method = (
        f"pass = rate >= {pass_t:g} of N; fail <= {fail_t:g}; else flaky. "
        "p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%."
    )
    return [
        method,
        f"full transcripts: runs/{result.candidate_run_id}/cases/<case>/rep_k.json",
    ]


def _verdict_summary(result: DiffResult, verdict: dict[str, Any]) -> list[str]:
    """Body lines of the verdict panel, minus the repair log and the headline itself."""
    name = str(verdict.get("verdict", "")).upper()
    restored = verdict.get("restored", 0)
    regressed_total = verdict.get("regressed_total", 0)
    broken = verdict.get("broken_by_patch", 0)
    lines: list[str] = []

    if name == "SAFE":
        lines.append("no regressed cases; the candidate model is a drop-in replacement.")
    elif name == "SAFE WITH PATCH":
        lines.append(
            f"restored {restored}/{regressed_total} regressed, {broken} previously-passing broken"
        )
    elif name == "STAY PINNED":
        remaining = _num(regressed_total) - _num(restored)
        still = [c.case_id for c in result.by_label(LABEL_REGRESSED)]
        reason = (
            f"reason: {remaining} of {regressed_total} regressed cases still fail after the "
            "repair budget"
        )
        if broken:
            reason += f"; {broken} previously-passing cases broken by the patch"
        lines.append(reason)
        if still:
            lines.append("still regressed: " + ", ".join(still[:12]))
    else:
        lines.append(
            f"restored {restored}/{regressed_total} regressed, {broken} previously-passing broken"
        )

    if verdict.get("patch_path"):
        lines.append(f"patch: {verdict['patch_path']}")
    return lines


def _repair_log(verdict: dict[str, Any]) -> list[str]:
    return [str(entry) for entry in (verdict.get("repair_log") or [])]


def _num(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Rich renderer
# ---------------------------------------------------------------------------


def render_diff(
    result: DiffResult,
    console: Console | None = None,
    verdict: dict[str, Any] | None = None,
) -> None:
    """Print the diff to a rich console (stdout by default)."""
    console = console or Console()
    provider, simulated = _providers(result)

    header = Text()
    header.append(_model_endpoint(result.baseline_manifest), style="bold")
    header.append("  ->  ")
    header.append(_model_endpoint(result.candidate_manifest), style="bold cyan")
    header.append(f"\nprovider: {provider}    n_reps: {_n_reps(result)}")
    header.append(
        f"\nbaseline: {result.baseline_run_id}\ncandidate: {result.candidate_run_id}",
        style="dim",
    )
    if result.agent_files_differ:
        header.append("\nagent files differ between runs (patched candidate)", style="yellow")
    if simulated:
        header.append("\n")
        header.append(f" {SIM_WARNING} ", style="bold white on red")
    console.print(Panel(header, title="upshift diff", border_style="cyan"))

    if verdict:
        name = str(verdict.get("verdict", "")).upper()
        color = _VERDICT_COLOR.get(name, "white")
        body = Text()
        body.append(name or "VERDICT", style=f"bold {color}")
        for line in _verdict_summary(result, verdict):
            body.append("\n" + line)
        log = _repair_log(verdict)
        if log:
            body.append("\nrepair log:", style="dim")
            for entry in log:
                body.append(f"\n  - {entry}")
        console.print(Panel(body, title="verdict", border_style=color))

    baseline_rate, candidate_rate = _suite_rates(result)
    console.print(f"baseline   {baseline_rate}")
    console.print(f"candidate  {candidate_rate}")
    console.print(_label_counts_line(result))

    rows = _detail_cases(result)
    if rows:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False, expand=False)
        table.add_column("case")
        table.add_column("label")
        table.add_column("base", justify="right")
        table.add_column("cand", justify="right")
        table.add_column("p", justify="right")
        table.add_column("signatures")
        table.add_column("first failing detail")
        for case in rows:
            case_id, lbl, base, cand, pval, sigs, detail = _row_cells(case)
            table.add_row(
                case_id,
                Text(lbl, style=_LABEL_COLOR.get(lbl, "white")),
                base,
                cand,
                pval,
                sigs,
                detail,
            )
        console.print()
        console.print(table)
    else:
        console.print()
        console.print("no non-stable-pass cases.", style="dim")

    console.print()
    for line in _footnote(result):
        console.print(line, style="dim")


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def diff_to_markdown(result: DiffResult, verdict: dict[str, Any] | None = None) -> str:
    """The same content as ``render_diff``, as plain markdown. No emojis."""
    provider, simulated = _providers(result)
    out: list[str] = []

    out.append("# upshift diff")
    out.append("")
    out.append(
        f"`{_model_endpoint(result.baseline_manifest)}`  ->  "
        f"`{_model_endpoint(result.candidate_manifest)}`"
    )
    out.append("")
    out.append(f"- provider: {provider}")
    out.append(f"- n_reps: {_n_reps(result)}")
    out.append(f"- baseline run: `{result.baseline_run_id}`")
    out.append(f"- candidate run: `{result.candidate_run_id}`")
    if result.agent_files_differ:
        out.append("- agent files differ between runs (patched candidate)")
    if simulated:
        out.append("")
        out.append(f"**{SIM_WARNING}**")

    if verdict:
        name = str(verdict.get("verdict", "")).upper() or "VERDICT"
        out.append("")
        out.append(f"## Verdict: {name}")
        for line in _verdict_summary(result, verdict):
            out.append("")
            out.append(line)
        log = _repair_log(verdict)
        if log:
            out.append("")
            out.append("repair log:")
            out.append("")
            out += [f"- {entry}" for entry in log]

    baseline_rate, candidate_rate = _suite_rates(result)
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- baseline: {baseline_rate}")
    out.append(f"- candidate: {candidate_rate}")
    out.append("")
    out.append(_label_counts_line(result))

    rows = _detail_cases(result)
    out.append("")
    out.append("## Cases that changed")
    out.append("")
    stable_pass = result.counts.get(LABEL_STABLE_PASS, 0)
    out.append(f"{stable_pass} stable-pass cases not listed.")
    out.append("")
    if rows:
        out.append("| case | label | base | cand | p | signatures | first failing detail |")
        out.append("| --- | --- | ---: | ---: | ---: | --- | --- |")
        for case in rows:
            out.append("| " + " | ".join(_md_cell(c) for c in _row_cells(case)) + " |")
    else:
        out.append("No non-stable-pass cases.")

    out.append("")
    for line in _footnote(result):
        out.append(line)
        out.append("")
    return "\n".join(out).rstrip() + "\n"
