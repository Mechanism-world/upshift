"""A capacity 429 is weather, not evidence.

rescue-ops recorded the same thing three times (`ghi56-019`, `ghi56-006`, `ghi56-021`): the
flex tier answered `429 Flex does not have sufficient resources`, the rep was written down as
a failing rep, and the case's pass rate — the number the verdict is computed from — took the
hit. Nothing about the model was measured; the queue was full.

So: 429 and 5xx and timeouts and connection errors are retried per rep with a bounded
exponential backoff (the SDKs retry too — this is the outer layer, around the whole episode),
and a rep that still fails is recorded with `type`/`error_type: "transient_provider_error"`,
which is a NON-BEHAVIOURAL class: the differ files it under `harness_error` and the verdict
refuses to conclude. Billing and auth are never retried — a 429 that says the account is out
of credit is a fact about the account and still aborts the run.

`--retry-errored` is the resume half: re-run only the reps whose recorded error was
non-behavioural, against a run already on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _verdict_support import write_agent

from upshift import differ, recorder, verdict
from upshift import runner as runner_module
from upshift.native import protocol as native_protocol
from upshift.providers.base import Provider, ProviderAPIError
from upshift.runner import BillingError, run_suite

MODEL = "m1"
CASES = ["c1"]


class FlakyProvider(Provider):
    """Raises a scripted sequence of errors per case, then answers "OK".

    `failures` is consumed one per API CALL, and these cases make exactly one call each, so
    `[error, error]` means the first two attempts fail and everything after succeeds — the
    shape a capacity blip has.
    """

    name = "sim"

    def __init__(self, failures: list[ProviderAPIError]) -> None:
        self.failures = list(failures)
        self.calls = 0

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {
            "model": request.get("model", MODEL),
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


def _capacity_429() -> ProviderAPIError:
    return ProviderAPIError(
        "Flex does not have sufficient resources to fulfill this request. Please try again.",
        status_code=429,
        error_type="api_status_error",
    )


def _gateway_502() -> ProviderAPIError:
    return ProviderAPIError("Bad gateway", status_code=502, error_type="api_status_error")


@pytest.fixture()
def no_sleep(monkeypatch):
    """The backoff is real time; the test asserts on the delays instead of waiting them out."""
    slept: list[float] = []
    monkeypatch.setattr(runner_module, "_sleep", slept.append)
    return slept


def _run(tmp_path: Path, provider, *, run_id="r1", n_reps=1, **kwargs) -> Path:
    agent = write_agent(tmp_path / "agent", CASES)
    return run_suite(
        agent, provider, run_id, n_reps=n_reps, model_override=MODEL,
        runs_root=tmp_path / "runs", workers=1, **kwargs,
    )


# ---------------------------------------------------------------------------
# The retry itself
# ---------------------------------------------------------------------------


def test_a_capacity_429_is_retried_and_the_rep_passes(tmp_path: Path, no_sleep) -> None:
    provider = FlakyProvider([_capacity_429()])
    directory = _run(tmp_path, provider)
    record = recorder.load_rep(directory, "c1", 1)
    assert record.passed is True
    assert record.api_error is None
    assert len(no_sleep) == 1, "one failed attempt, one backoff"


def test_5xx_and_timeouts_are_retried_too(tmp_path: Path, no_sleep) -> None:
    provider = FlakyProvider(
        [_gateway_502(), ProviderAPIError("Request timed out.", status_code=None)]
    )
    record = recorder.load_rep(_run(tmp_path, provider), "c1", 1)
    assert record.passed is True
    assert len(no_sleep) == 2


def test_the_retry_budget_is_three_attempts(tmp_path: Path, no_sleep) -> None:
    provider = FlakyProvider([_capacity_429() for _ in range(10)])
    _run(tmp_path, provider)
    assert len(no_sleep) == runner_module.RETRY_MAX_ATTEMPTS - 1
    assert sum(no_sleep) <= runner_module.RETRY_MAX_TOTAL_S


def test_the_backoff_is_exponential_and_jittered(tmp_path: Path, no_sleep) -> None:
    provider = FlakyProvider([_capacity_429() for _ in range(10)])
    _run(tmp_path, provider)
    assert no_sleep[1] > no_sleep[0], "the second wait must be longer than the first"
    # Jitter: the delays are not the bare powers of two, or every client in a capacity
    # incident retries in lockstep and the queue never drains.
    assert no_sleep[0] != runner_module.RETRY_BASE_DELAY_S


def test_an_unrecovered_transient_error_is_recorded_as_non_behavioural(
    tmp_path: Path, no_sleep
) -> None:
    provider = FlakyProvider([_capacity_429() for _ in range(10)])
    record = recorder.load_rep(_run(tmp_path, provider), "c1", 1)
    assert record.passed is False
    assert record.api_error["type"] == runner_module.TRANSIENT_PROVIDER_ERROR
    assert record.api_error["error_type"] == runner_module.TRANSIENT_PROVIDER_ERROR
    # The provider's own words and status survive: this is still the evidence of what
    # happened, it is only the CLASS that upshift decides.
    assert record.api_error["status_code"] == 429
    assert "Flex does not have sufficient resources" in record.api_error["message"]
    assert record.api_error["attempts"] == runner_module.RETRY_MAX_ATTEMPTS
    assert native_protocol.is_non_behavioural(record.api_error)


def test_the_differ_files_it_under_harness_error(tmp_path: Path, no_sleep) -> None:
    record = recorder.load_rep(
        _run(tmp_path, FlakyProvider([_capacity_429() for _ in range(10)])), "c1", 1
    )
    assert differ.SIG_HARNESS_ERROR in differ.failure_signatures([record], None)


def test_the_verdict_refuses_to_conclude_on_it(tmp_path: Path, no_sleep) -> None:
    record = recorder.load_rep(
        _run(tmp_path, FlakyProvider([_capacity_429() for _ in range(10)])), "c1", 1
    )
    assert verdict.classify_api_error(record.api_error) == verdict.REASON_TRANSIENT_PROVIDER
    assert verdict.REASON_TRANSIENT_PROVIDER in verdict.INCONCLUSIVE_REASONS


# ---------------------------------------------------------------------------
# What is NOT retried
# ---------------------------------------------------------------------------


def test_a_billing_429_is_not_retried_and_still_aborts(tmp_path: Path, no_sleep) -> None:
    """`insufficient_quota` arrives as a 429 too. Retrying it burns wall-clock to reach the
    same refusal, and recording it would be a lie about the model either way."""
    provider = FlakyProvider(
        [
            ProviderAPIError(
                "You exceeded your current quota, please check your plan and billing details.",
                status_code=429,
                error_type="insufficient_quota",
            )
        ]
    )
    with pytest.raises(BillingError):
        _run(tmp_path, provider)
    assert no_sleep == []


def test_an_auth_401_is_not_retried(tmp_path: Path, no_sleep) -> None:
    provider = FlakyProvider(
        [ProviderAPIError("Incorrect API key provided.", status_code=401)]
    )
    record = recorder.load_rep(_run(tmp_path, provider), "c1", 1)
    assert no_sleep == []
    assert record.api_error["type"] != runner_module.TRANSIENT_PROVIDER_ERROR


def test_a_400_from_the_model_is_never_retried(tmp_path: Path, no_sleep) -> None:
    """The whole product is built on 400s being evidence. Retrying one, or reclassifying it
    as weather, would erase exactly the regression upshift exists to find."""
    provider = FlakyProvider(
        [
            ProviderAPIError(
                "Function tools with reasoning_effort are not supported.",
                status_code=400,
                error_type="api_status_error",
            )
        ]
    )
    record = recorder.load_rep(_run(tmp_path, provider), "c1", 1)
    assert no_sleep == []
    assert record.api_error["status_code"] == 400
    assert differ.SIG_API_ERROR_TOOLS_REASONING in differ.failure_signatures([record], None)


# ---------------------------------------------------------------------------
# --retry-errored
# ---------------------------------------------------------------------------


def test_retry_errored_reruns_only_the_non_behavioural_reps(tmp_path: Path, no_sleep) -> None:
    provider = FlakyProvider([_capacity_429() for _ in range(10)])
    directory = _run(tmp_path, provider, n_reps=2)
    assert all(
        recorder.load_rep(directory, "c1", rep).api_error["type"]
        == runner_module.TRANSIENT_PROVIDER_ERROR
        for rep in (1, 2)
    )

    healthy = FlakyProvider([])
    _run(tmp_path, healthy, n_reps=2, retry_errored=True)
    for rep in (1, 2):
        record = recorder.load_rep(directory, "c1", rep)
        assert record.passed is True
        assert record.api_error is None
        assert record.retried_from["api_error"]["type"] == (
            runner_module.TRANSIENT_PROVIDER_ERROR
        )


def test_without_the_flag_an_errored_rep_is_left_alone(tmp_path: Path, no_sleep) -> None:
    """Resume must stay resume: a completed rep is not silently re-billed."""
    directory = _run(tmp_path, FlakyProvider([_capacity_429() for _ in range(10)]))
    healthy = FlakyProvider([])
    _run(tmp_path, healthy, retry_errored=False)
    assert healthy.calls == 0
    assert recorder.load_rep(directory, "c1", 1).passed is False


def test_a_passing_rep_is_never_retried(tmp_path: Path, no_sleep) -> None:
    directory = _run(tmp_path, FlakyProvider([]))
    assert recorder.load_rep(directory, "c1", 1).passed is True
    second = FlakyProvider([])
    _run(tmp_path, second, retry_errored=True)
    assert second.calls == 0


def test_a_retry_that_fails_transiently_again_keeps_the_original_record(
    tmp_path: Path, no_sleep
) -> None:
    """Overwriting would lose the first record's attempt count and its provider message for
    nothing: the new outcome says exactly what the old one said."""
    directory = _run(tmp_path, FlakyProvider([_capacity_429() for _ in range(10)]))
    before = json.loads(recorder.rep_path(directory, "c1", 1).read_text())
    _run(tmp_path, FlakyProvider([_gateway_502() for _ in range(10)]), retry_errored=True)
    assert json.loads(recorder.rep_path(directory, "c1", 1).read_text()) == before


def test_a_retry_that_reaches_a_real_failure_does_overwrite(tmp_path: Path, no_sleep) -> None:
    """A new NON-transient outcome is a new measurement and replaces the old non-answer —
    including a 400, which is the regression this product is for."""
    directory = _run(tmp_path, FlakyProvider([_capacity_429() for _ in range(10)]))
    _run(
        tmp_path,
        FlakyProvider(
            [
                ProviderAPIError(
                    "Function tools with reasoning_effort are not supported.",
                    status_code=400,
                    error_type="api_status_error",
                )
            ]
        ),
        retry_errored=True,
    )
    record = recorder.load_rep(directory, "c1", 1)
    assert record.api_error["status_code"] == 400
    assert record.retried_from["api_error"]["type"] == runner_module.TRANSIENT_PROVIDER_ERROR


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_the_flag_is_on_both_resuming_subcommands(capsys) -> None:
    from upshift import cli

    for subcommand in ("run", "upgrade"):
        with pytest.raises(SystemExit) as exit_info:
            cli.main([subcommand, "--help"])
        assert exit_info.value.code == 0
        assert "--retry-errored" in capsys.readouterr().out


def test_run_forwards_retry_errored_to_the_suite(tmp_path: Path, monkeypatch, no_sleep) -> None:
    """The flag has to reach `run_suite`; a CLI option that parses and does nothing is worse
    than no option."""
    from upshift import cli

    agent = write_agent(tmp_path / "agent", CASES)
    seen: dict[str, Any] = {}
    real = runner_module.run_suite

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_suite", spy)
    monkeypatch.setattr(cli, "_make_provider", lambda args: FlakyProvider([]))
    monkeypatch.setattr(cli, "_check_models", lambda *a, **k: None)
    code = cli.main(
        [
            "run", "--agent", str(agent), "--model", MODEL,
            "--run-id", "r1", "--runs-root", str(tmp_path / "runs"), "--n", "1",
            "--quiet", "--retry-errored",
        ]
    )
    assert code == 0
    assert seen["retry_errored"] is True
