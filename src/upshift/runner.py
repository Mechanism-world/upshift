"""Execute an eval suite against a named model/config, N reps per case, recording everything.

Resumable: reps whose record file already exists (valid, with a `passed` key) are skipped, so
an interrupted run continues where it left off.
"""

from __future__ import annotations

import hashlib
import importlib.util
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from upshift import recorder
from upshift.agent_loop import run_episode
from upshift.checks import evaluate_checks
from upshift.providers import Provider
from upshift.schemas import AgentConfig, Case, RepRecord


def load_backend_factory(agent_dir: str | Path) -> Callable[[dict], Any]:
    """The victim agent dir must contain backend.py exposing create_backend(initial_state)."""
    path = Path(agent_dir) / "backend.py"
    spec = importlib.util.spec_from_file_location(f"victim_backend_{Path(agent_dir).name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.create_backend


def seed_for(case_id: str, rep: int) -> int:
    return int(hashlib.sha256(f"{case_id}:{rep}".encode()).hexdigest()[:8], 16)


def run_suite(
    agent_dir: str | Path,
    provider: Provider,
    run_id: str,
    *,
    n_reps: int = 5,
    model_override: str | None = None,
    params_override: dict[str, Any] | None = None,
    endpoint_override: str | None = None,
    runs_root: str | Path = recorder.DEFAULT_RUNS_ROOT,
    case_ids: list[str] | None = None,
    workers: int = 4,
    notes: str = "",
    on_rep_done: Callable[[RepRecord], None] | None = None,
) -> Path:
    """Run every case n_reps times; returns the run directory."""
    config = AgentConfig.load(agent_dir)
    cases = Case.load_all(Path(agent_dir) / "cases" / "cases.json")
    effective_model = model_override or config.model
    effective_endpoint = endpoint_override or config.endpoint
    effective_params = dict(config.params) if params_override is None else dict(params_override)

    run_directory = recorder.run_dir(runs_root, run_id)
    recorder.write_manifest(
        run_directory,
        run_id=run_id,
        provider=provider.name,
        config=config,
        model_requested=effective_model,
        endpoint=effective_endpoint,
        params=effective_params,
        n_reps=n_reps,
        cases=cases,
        notes=notes,
    )

    selected = cases if case_ids is None else [c for c in cases if c.id in set(case_ids)]
    if case_ids is not None and len(selected) != len(set(case_ids)):
        missing = set(case_ids) - {c.id for c in selected}
        raise ValueError(f"unknown case ids: {sorted(missing)}")
    backend_factory = load_backend_factory(agent_dir)

    todo = [
        (case, rep)
        for case in selected
        for rep in range(1, n_reps + 1)
        if not recorder.is_rep_complete(run_directory, case.id, rep)
    ]

    # Wave-batching providers (see providers/openai_batch.py) need every episode running
    # concurrently and pre-registered, so a wave can be declared complete.
    batching = getattr(provider, "requires_all_workers", False)
    if batching and todo:
        workers = len(todo)
        provider.episodes_starting([f"{case.id}:{rep}" for case, rep in todo])

    def one(case: Case, rep: int) -> RepRecord:
        try:
            return _one_inner(case, rep)
        finally:
            if batching:
                provider.episode_finished(f"{case.id}:{rep}")

    def _one_inner(case: Case, rep: int) -> RepRecord:
        start = time.monotonic()
        backend = backend_factory(case.initial_state)
        episode = run_episode(
            config,
            case,
            provider,
            backend,
            rep=rep,
            seed=seed_for(case.id, rep),
            model_override=effective_model,
            params_override=effective_params,
            endpoint_override=effective_endpoint,
        )
        check_results, passed = evaluate_checks(
            case,
            api_error=episode.api_error,
            tool_executions=episode.tool_executions,
            final_state=episode.final_state,
            final_message=episode.final_message,
        )
        record = RepRecord(
            case_id=case.id,
            rep=rep,
            seed=seed_for(case.id, rep),
            model_requested=effective_model,
            resolved_model=episode.resolved_model,
            endpoint=effective_endpoint,
            params=effective_params,
            api_calls=episode.api_calls,
            tool_executions=episode.tool_executions,
            final_state=episode.final_state,
            final_message=episode.final_message,
            check_results=check_results,
            passed=passed,
            api_error=episode.api_error,
            usage=episode.usage,
            latency_s=round(time.monotonic() - start, 3),
        )
        recorder.write_rep(run_directory, record)
        return record

    if workers <= 1:
        for case, rep in todo:
            record = one(case, rep)
            if on_rep_done:
                on_rep_done(record)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, case, rep): (case.id, rep) for case, rep in todo}
            for future in as_completed(futures):
                record = future.result()
                if on_rep_done:
                    on_rep_done(record)

    recorder.write_summary(run_directory)
    return run_directory
