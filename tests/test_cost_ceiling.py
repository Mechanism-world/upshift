"""`--max-cost-usd` on `upshift run` and `upshift upgrade`.

The ceiling has to be exercised without spending anything, so these tests run the sim
provider against a fake pricing table: `pricing.ceiling_price` prices whatever the rate
table lists, whoever the provider is, precisely so a spend ceiling is testable for free.
The sim's recorded usage numbers are real token counts, so the arithmetic under test is
the same arithmetic a paid run does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from upshift import budget, cli, pricing
from upshift.budget import CostCeiling, CostCeilingExceeded
from upshift.providers.sim import SimProvider
from upshift.runner import run_suite
from upshift.schemas import Case

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "tests" / "todo_agent"
CASE_IDS = sorted(c.id for c in Case.load_all(AGENT_DIR / "cases" / "cases.json"))

#: Absurd rates so a handful of simulated reps crosses a cent-sized ceiling. Injected into
#: the real table by `expensive_sim`, never shipped.
FAKE_RATE = (1_000.0, 1_000.0)


@pytest.fixture
def expensive_sim(monkeypatch):
    for model in ("sim-5.5", "sim-5.6-sol"):
        monkeypatch.setitem(pricing.RATES, model, FAKE_RATE)
    return FAKE_RATE


def _reps_on_disk(run_directory: Path) -> int:
    return len(list(run_directory.glob("cases/*/rep_*.json")))


def test_dispatch_stops_once_the_ceiling_is_crossed(tmp_path, expensive_sim):
    runs_root = tmp_path / "runs"
    ceiling = CostCeiling(runs_root=runs_root, prefix="capped", limit_usd=0.02)
    with pytest.raises(CostCeilingExceeded) as raised:
        run_suite(
            AGENT_DIR,
            SimProvider(),
            "capped",
            n_reps=5,
            model_override="sim-5.5",
            runs_root=runs_root,
            workers=1,
            cost_ceiling=ceiling,
        )
    stop = raised.value
    assert stop.spent_usd >= 0.02
    assert stop.limit_usd == 0.02
    assert not stop.unpriced_models  # the injected table prices every model in play

    # It stopped early: some reps ran, most did not.
    run_directory = runs_root / "capped"
    written = _reps_on_disk(run_directory)
    assert 0 < written < len(CASE_IDS) * 5

    # And the priced total the ceiling enforced is the one `upshift cost` reports.
    on_disk, unpriced = budget.spent_on_disk(runs_root, "capped")
    assert not unpriced
    assert on_disk == pytest.approx(stop.spent_usd, rel=1e-9)


def test_stopping_leaves_the_run_resumable(tmp_path, expensive_sim):
    runs_root = tmp_path / "runs"
    with pytest.raises(CostCeilingExceeded):
        run_suite(
            AGENT_DIR, SimProvider(), "capped", n_reps=5, model_override="sim-5.5",
            runs_root=runs_root, workers=1,
            cost_ceiling=CostCeiling(runs_root=runs_root, prefix="capped", limit_usd=0.02),
        )
    run_directory = runs_root / "capped"
    after_stop = _reps_on_disk(run_directory)
    kept = {
        path: path.read_text() for path in sorted(run_directory.glob("cases/*/rep_*.json"))
    }
    spent_after_stop, _ = budget.spent_on_disk(runs_root, "capped")

    # Rerunning the same command with a bigger ceiling finishes the run...
    resumed = CostCeiling(runs_root=runs_root, prefix="capped", limit_usd=1_000.0)
    assert resumed.spent_usd == pytest.approx(spent_after_stop, rel=1e-9)
    run_suite(
        AGENT_DIR, SimProvider(), "capped", n_reps=5, model_override="sim-5.5",
        runs_root=runs_root, workers=1, cost_ceiling=resumed,
    )
    assert _reps_on_disk(run_directory) == len(CASE_IDS) * 5
    assert _reps_on_disk(run_directory) > after_stop
    # ...without re-running, or rewriting, anything already recorded.
    for path, content in kept.items():
        assert path.read_text() == content
    assert (run_directory / "summary.json").is_file()


def test_ceiling_counts_every_run_under_an_upgrade_tag(tmp_path, expensive_sim):
    """`upgrade` derives every run id from --tag, so the whole family shares one budget."""
    runs_root = tmp_path / "runs"
    for run_id in ("t-baseline", "t-c01-abcd1234-verify"):
        run_suite(
            AGENT_DIR, SimProvider(), run_id, n_reps=1, model_override="sim-5.5",
            runs_root=runs_root, workers=1, case_ids=[CASE_IDS[0]],
        )
    one_run, _ = budget.spent_on_disk(runs_root, "t-baseline")
    whole_tag, _ = budget.spent_on_disk(runs_root, "t", descendants=True)
    assert whole_tag > one_run > 0
    # An unrelated run id that merely starts with the same letters is not swept in.
    assert budget.spent_on_disk(runs_root, "t", descendants=False)[0] == 0.0


def test_unpriced_models_fail_closed_and_are_reported():
    """A model with no published rate must never be spent as $0."""
    assert pricing.price("openai", "gpt-99-unknown", 1_000_000, 0, 0) is None
    usd, published = pricing.ceiling_price("openai", "gpt-99-unknown", 1_000_000, 0, 0)
    assert published is False
    assert usd == pytest.approx(pricing.highest_rate()[0])
    # ...and the user is warned about it before the first call, not after the bill.
    warning = budget.startup_warning("openai", ["gpt-99-unknown"])
    assert warning and "gpt-99-unknown" in warning and "no published rate" in warning
    assert budget.startup_warning("openai", ["gpt-5.5"]) is None
    # The simulator issues no request, so an unpriced sim model is $0, not blindness.
    assert budget.startup_warning("sim", ["sim-5.5"]) is None
    assert pricing.ceiling_price("sim", "sim-5.5", 10**9, 10**9, 0) == (0.0, True)


def test_unpriced_usage_is_charged_at_the_highest_known_rate(tmp_path, monkeypatch):
    """The ceiling still fires on an unpriced model, and says which model it was blind on."""
    monkeypatch.setitem(pricing.TIER_MULTIPLIER, "sim", 1.0)
    ceiling = CostCeiling(runs_root=tmp_path, prefix="blind", limit_usd=0.01)
    ceiling.observe("openai", "gpt-99-unknown", {"input_tokens": 10_000, "output_tokens": 0})
    assert ceiling.unpriced_models == ["gpt-99-unknown"]
    with pytest.raises(CostCeilingExceeded) as raised:
        ceiling.check("candidate run")
    assert raised.value.unpriced_models == ["gpt-99-unknown"]
    assert "no published rate" in str(raised.value)


def test_ceiling_rejects_a_non_positive_limit(tmp_path):
    with pytest.raises(ValueError, match="must be positive"):
        CostCeiling(runs_root=tmp_path, prefix="x", limit_usd=0)


# --- CLI ---------------------------------------------------------------------------------


def _upgrade_argv(runs_root: Path, limit: str | None) -> list[str]:
    argv = [
        "upgrade", "--agent", str(AGENT_DIR), "--provider", "sim",
        "--baseline-model", "sim-5.5", "--candidate-model", "sim-5.6-sol",
        "--tag", "capped", "--n", "2", "--workers", "1", "--quiet",
        "--runs-root", str(runs_root),
    ]
    return argv if limit is None else argv + ["--max-cost-usd", limit]


def test_upgrade_stops_without_emitting_a_verdict(tmp_path, expensive_sim, capsys):
    runs_root = tmp_path / "runs"
    assert cli.main(_upgrade_argv(runs_root, "0.02")) == cli.EXIT_COST_STOPPED

    tag_dir = runs_root / "capped"
    marker = tag_dir / budget.COST_STOPPED_FILE
    assert marker.is_file(), "a stopped pipeline must be marked as stopped"
    payload = json.loads(marker.read_text())
    assert payload["status"] == "COST_STOPPED"
    assert payload["limit_usd"] == 0.02
    assert payload["spent_usd"] >= 0.02
    assert payload["phase"]
    assert payload["tag"] == "capped"

    # No verdict, no diff, no report: the pipeline did not finish and must not look like it.
    assert not (tag_dir / "verdict.json").exists()
    assert not (tag_dir / "diff.json").exists()
    assert not (tag_dir / "REPORT.md").exists()

    out = capsys.readouterr().out
    assert "cost ceiling reached" in out
    assert "no verdict" in out


def test_upgrade_resumes_and_clears_the_marker(tmp_path, expensive_sim):
    runs_root = tmp_path / "runs"
    assert cli.main(_upgrade_argv(runs_root, "0.02")) == cli.EXIT_COST_STOPPED
    tag_dir = runs_root / "capped"
    assert (tag_dir / budget.COST_STOPPED_FILE).is_file()
    baseline_reps = _reps_on_disk(runs_root / "capped-baseline")
    assert baseline_reps > 0

    # Same command, ceiling lifted: it resumes from what is on disk and finishes.
    assert cli.main(_upgrade_argv(runs_root, "1000")) in (0, 1)
    assert _reps_on_disk(runs_root / "capped-baseline") == len(CASE_IDS) * 2
    assert (tag_dir / "verdict.json").is_file()
    assert not (tag_dir / budget.COST_STOPPED_FILE).exists(), (
        "a finished pipeline must not leave a stale COST_STOPPED marker"
    )


def test_run_stops_and_marks_the_run_directory(tmp_path, expensive_sim):
    runs_root = tmp_path / "runs"
    argv = [
        "run", "--agent", str(AGENT_DIR), "--provider", "sim", "--model", "sim-5.5",
        "--run-id", "capped", "--n", "5", "--workers", "1", "--quiet",
        "--runs-root", str(runs_root),
    ]
    assert cli.main([*argv, "--max-cost-usd", "0.02"]) == cli.EXIT_COST_STOPPED
    marker = runs_root / "capped" / budget.COST_STOPPED_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text())["run_id"] == "capped"

    assert cli.main([*argv, "--max-cost-usd", "1000"]) == 0
    assert not marker.exists()
    assert _reps_on_disk(runs_root / "capped") == len(CASE_IDS) * 5


def test_run_rejects_a_non_positive_ceiling(tmp_path):
    code = cli.main([
        "run", "--agent", str(AGENT_DIR), "--provider", "sim", "--run-id", "x",
        "--runs-root", str(tmp_path / "runs"), "--max-cost-usd", "0",
    ])
    assert code == 2


def test_no_ceiling_by_default(tmp_path):
    """The flag is opt-in: without it nothing about an existing run changes."""
    runs_root = tmp_path / "runs"
    code = cli.main([
        "run", "--agent", str(AGENT_DIR), "--provider", "sim", "--model", "sim-5.5",
        "--run-id", "free", "--n", "1", "--workers", "1", "--quiet",
        "--runs-root", str(runs_root),
    ])
    assert code == 0
    assert not (runs_root / "free" / budget.COST_STOPPED_FILE).exists()
