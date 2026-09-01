"""Wiring: the extraction engine on top of the normal provider machinery, recorded on disk.

Every extraction attempt is written as a rep record under `runs/adapt-<name>/` in exactly the
shape `recorder.py` writes and `pricing.run_cost` reads, so `upshift cost` prices an adapt run
with no changes to pricing.py: one "case" (`extraction`) whose reps are the attempts.

The cost guard runs *before* each call: the request is priced at its estimated token count
plus an output allowance, and the run aborts rather than exceed `--max-cost-usd`.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from upshift import __version__, recorder
from upshift.adapt import AdaptAborted
from upshift.adapt.extract import ExtractionResult
from upshift.adapt.report import CostInfo
from upshift.providers.base import Provider
from upshift.schemas import APICall, CheckResult, RepRecord

CASE_ID = "extraction"
#: Tokens we assume an extraction reply may cost when pricing a call before making it.
OUTPUT_ALLOWANCE_TOKENS = 12_000


def run_id_for(name: str) -> str:
    return f"adapt-{name}"


class RecordingExtractor:
    """Callable `call_model` for `extract.extract`, recording every attempt."""

    def __init__(
        self,
        provider: Provider,
        *,
        model: str,
        run_id: str,
        runs_root: str | Path = recorder.DEFAULT_RUNS_ROOT,
        max_cost_usd: float | None = None,
        source: str = "",
        commit: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.run_id = run_id
        self.run_dir = recorder.run_dir(runs_root, run_id)
        self.max_cost_usd = max_cost_usd
        self.source = source
        self.commit = commit
        self.records: list[RepRecord] = []
        self._calls = 0

    # -- manifest -----------------------------------------------------------

    def start(self, *, evidence_tokens: int, out_dir: str) -> None:
        """Write the run manifest. Same shape as recorder.write_manifest, minus the agent
        config an adapt run does not have yet (that is the output, not the input)."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": self.run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "provider": self.provider.name,
            "agent": {
                "name": self.run_id,
                "endpoint": "chat_completions",
                "model_requested": self.model,
                "params": {},
                "max_turns": 1,
                "file_hashes": {},
            },
            "n_reps": 0,
            "thresholds": dict(recorder.THRESHOLDS),
            "case_set_hash": "",
            "upshift_version": __version__,
            "notes": "upshift adapt: extraction over source evidence, not an eval run",
            "adapt": {
                "source": self.source,
                "commit": self.commit,
                "out_dir": out_dir,
                "evidence_tokens": evidence_tokens,
                "max_cost_usd": self.max_cost_usd,
            },
        }
        (self.run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))

    # -- cost ---------------------------------------------------------------

    def spent_usd(self) -> float:
        from upshift.pricing import price

        usage = self.total_usage()
        return price(
            self.provider.name, self.model, usage["input_tokens"], usage["output_tokens"],
            usage["cached_input_tokens"],
        ) or 0.0

    def total_usage(self) -> dict[str, int]:
        total = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
        for record in self.records:
            for key in total:
                total[key] += record.usage.get(key, 0)
        return total

    def _guard(self, request: dict[str, Any]) -> None:
        if self.max_cost_usd is None:
            return
        from upshift.pricing import price

        estimated_input = len(json.dumps(request.get("messages") or [])) // 4
        projected = price(
            self.provider.name, self.model, estimated_input, OUTPUT_ALLOWANCE_TOKENS, 0
        )
        if projected is None:  # unknown rate: cannot promise a bound, so do not pretend to
            return
        total = self.spent_usd() + projected
        if total > self.max_cost_usd:
            raise AdaptAborted(
                f"extraction would cost about ${total:.2f} (already spent ${self.spent_usd():.2f}, "
                f"this call ~${projected:.2f}) which is over --max-cost-usd "
                f"{self.max_cost_usd:.2f}. Nothing further was sent. Narrow the input "
                f"(--max-evidence-tokens) or raise the budget.",
                stage="extract",
            )

    # -- the call -----------------------------------------------------------

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self._guard(request)
        self._calls += 1
        rep = self._calls
        start = time.monotonic()
        try:
            response = self.provider.call(
                "chat_completions", request, seed_key=f"{CASE_ID}:{rep}:0"
            )
        except Exception as exc:  # recorded, then re-raised for the CLI to render
            self._record(rep, request, None, error=exc, latency=time.monotonic() - start)
            raise
        self._record(rep, request, response, error=None, latency=time.monotonic() - start)
        return response

    def _record(
        self,
        rep: int,
        request: dict[str, Any],
        response: dict[str, Any] | None,
        *,
        error: Exception | None,
        latency: float,
    ) -> None:
        from upshift.agent_loop import _accumulate_usage
        from upshift.runner import seed_for

        usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
        if response:
            _accumulate_usage(usage, "chat_completions", response)
        api_error = None
        if error is not None:
            to_dict = getattr(error, "to_dict", None)
            api_error = to_dict() if callable(to_dict) else {
                "status_code": None, "message": str(error), "type": type(error).__name__
            }
        record = RepRecord(
            case_id=CASE_ID,
            rep=rep,
            seed=seed_for(CASE_ID, rep),
            model_requested=self.model,
            resolved_model=(response or {}).get("model"),
            endpoint="chat_completions",
            params={},
            api_calls=[
                APICall(endpoint="chat_completions", request=request, response=response,
                        error=api_error)
            ],
            tool_executions=[],
            final_state={},
            final_message="",
            check_results=[],
            passed=False,
            api_error=api_error,
            usage=usage,
            latency_s=round(latency, 3),
        )
        self.records.append(record)
        recorder.write_rep(self.run_dir, record)

    # -- after extraction ---------------------------------------------------

    def finalize(self, extraction: ExtractionResult | None) -> None:
        """Fold the schema verdict of each attempt into its record, then write summary.json."""
        by_rep = {attempt.index: attempt for attempt in (extraction.attempts if extraction else [])}
        for record in self.records:
            attempt = by_rep.get(record.rep)
            if attempt is None:
                continue
            record.final_message = attempt.text
            record.passed = attempt.ok
            record.check_results = [
                CheckResult(
                    check={"type": "extraction_schema_valid"},
                    passed=attempt.ok,
                    detail="; ".join(attempt.errors[:5]) if attempt.errors else "schema satisfied",
                )
            ]
            recorder.write_rep(self.run_dir, record)
        if self.records:
            recorder.write_summary(self.run_dir)

    def cost_info(self) -> CostInfo:
        from upshift.pricing import run_cost

        summary = run_cost(self.run_dir)
        return CostInfo(
            provider=summary["provider"],
            model=summary["model"],
            input_tokens=summary["input_tokens"],
            output_tokens=summary["output_tokens"],
            cached_input_tokens=summary["cached_input_tokens"],
            usd=summary["usd"],
            run_dir=str(self.run_dir.resolve()),
            run_id=self.run_id,
        )
