"""Execute an eval suite against a named model/config, N reps per case, recording everything.

Resumable: reps whose record file already exists (valid, with a `passed` key) are skipped, so
an interrupted run continues where it left off.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from upshift import recorder
from upshift.agent_loop import run_episode
from upshift.budget import CostCeiling, CostCeilingExceeded
from upshift.checks import evaluate_checks
from upshift.native import protocol as native_protocol
from upshift.native.exec import RunnerOptions
from upshift.native.runner import run_native_episode
from upshift.providers import Provider
from upshift.schemas import (
    SCOPE_ADAPTED_AGENT,
    SCOPE_NATIVE_APPLICATION,
    SCOPE_REQUEST_CONTRACT,
    AgentConfig,
    Case,
    RepRecord,
)

# An API error that means "the account cannot pay" is not evidence about the model. A rep
# hitting it aborts the whole run (nothing written) instead of recording N junk failures
# that a later diff would read as a regression. Seen live on both providers.
BILLING_ERROR_RE = re.compile(
    r"credit balance|insufficient_quota|exceeded your current quota|billing|"
    r"purchase credits|payment required",
    re.IGNORECASE,
)


class BillingError(ValueError):
    """Raised by run_suite when a provider reports a billing/quota failure."""


def _billing_message(api_error: object) -> str | None:
    if not api_error:
        return None
    message = api_error.get("message", "") if isinstance(api_error, dict) else str(api_error)
    return message if BILLING_ERROR_RE.search(str(message)) else None


def load_backend_factory(agent_dir: str | Path) -> Callable[[dict], Any]:
    """An agent dir must contain backend.py exposing create_backend(initial_state). ADAPTER.md."""
    path = Path(agent_dir) / "backend.py"
    if not path.is_file():
        raise ValueError(f"agent dir {agent_dir} has no backend.py (see ADAPTER.md)")
    spec = importlib.util.spec_from_file_location(f"upshift_backend_{Path(agent_dir).name}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{path} could not be loaded as a Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "create_backend", None)
    if factory is None:
        raise ValueError(f"{path} does not expose create_backend(initial_state) (see ADAPTER.md)")
    return factory


def seed_for(case_id: str, rep: int) -> int:
    return int(hashlib.sha256(f"{case_id}:{rep}".encode()).hexdigest()[:8], 16)


#: The marker `adapt` writes into a generated backend for a tool whose semantics it could not
#: re-implement (adapt/generate.py). A backend made only of these executes no tool semantics.
_ADAPT_STUB_MARKER = "TODO(adapt): not implemented"
#: The capture replay backend's data file (capture/adapt.py): its presence means the backend
#: answers from a recording rather than from an implementation.
_REPLAY_FILE = "recorded_tools.json"


def verification_scope(agent_dir: str | Path, config: AgentConfig) -> str:
    """The run's `scope` (DESIGN.md §A). Derived from the directory, never declared.

    - `native_application` — there is a `runner` block, so the application's own entry point
      runs, with its own request-building code.
    - `request_contract` — the backend is a capture replay, or every tool it exposes is an
      `adapt` stub. Nothing about the world was exercised: what such a run proves is that the
      provider accepts (or rejects) this request shape.
    - `adapted_agent` — the backend executes real tool semantics.

    The asymmetry runs the same way the simulated-provider stamp does (CLAUDE.md, 2026-09-03):
    calling an adapted agent `request_contract` understates a result, calling a replay
    `adapted_agent` tells a reader that behaviour was verified when it was not. So the two
    weaker labels are the ones this function reaches for on any doubt.
    """
    if config.runner is not None:
        return SCOPE_NATIVE_APPLICATION
    agent_dir = Path(agent_dir)
    if (agent_dir / _REPLAY_FILE).is_file():
        return SCOPE_REQUEST_CONTRACT
    backend_path = agent_dir / "backend.py"
    if backend_path.is_file():
        try:
            source = backend_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return SCOPE_ADAPTED_AGENT
        names = [
            tool.get("function", {}).get("name")
            for tool in config.tools or []
            if isinstance(tool, dict)
        ]
        names = [name for name in names if isinstance(name, str)]
        if _ADAPT_STUB_MARKER in source and names and all(
            f'"{name}"' not in source and f"'{name}'" not in source for name in names
        ):
            # Every declared tool falls through to the stub handler: no tool semantics exist.
            return SCOPE_REQUEST_CONTRACT
    return SCOPE_ADAPTED_AGENT


def episode_source_for(agent_dir: str | Path, config: AgentConfig) -> str:
    """The rep-level companion of `scope` (schemas.RepRecord.episode_source).

    Narrower than `scope` on purpose: `recorded_playback` is claimed only for a backend that
    genuinely replays recorded traffic, so an `adapt` stub agent (also `request_contract`)
    stays `live_model` — its requests really did go to a model, it is the TOOLS that answer
    nothing.
    """
    if config.runner is not None:
        return native_protocol.SOURCE_NATIVE_APPLICATION
    if (Path(agent_dir) / _REPLAY_FILE).is_file():
        return native_protocol.SOURCE_RECORDED_PLAYBACK
    return native_protocol.SOURCE_LIVE_MODEL


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
    cost_ceiling: CostCeiling | None = None,
    runner_options: RunnerOptions | None = None,
    capture_session: Any | None = None,
) -> Path:
    """Run every case n_reps times; returns the run directory.

    `cost_ceiling`, when given, is consulted before each rep is dispatched and charged after
    each rep is recorded; crossing it raises CostCeilingExceeded instead of making the next
    API call (see budget.py).

    An agent whose `agent.json` carries a `runner` block (DESIGN.md §C) is executed through
    `native.runner.run_native_episode` instead of the agent loop — the ONLY difference. The
    reps, the checks, the records, the statistics and the verdict are the same engine, which
    is the requirement the native path exists under: no second verification engine.
    `runner_options` carries the `--allow-runner` authorization; without it a native agent
    raises `RunnerNotAuthorized` rather than executing anything.
    """
    config = AgentConfig.load(agent_dir)
    cases_path = Path(agent_dir) / "cases" / "cases.json"
    if not cases_path.is_file():
        raise ValueError(f"agent dir {agent_dir} has no cases/cases.json (see ADAPTER.md)")
    cases = Case.load_all(cases_path)
    effective_model = model_override or config.model
    effective_endpoint = endpoint_override or config.endpoint
    effective_params = dict(config.params) if params_override is None else dict(params_override)

    native = config.runner is not None
    scope = verification_scope(agent_dir, config)
    episode_source = episode_source_for(agent_dir, config)

    run_directory = recorder.run_dir(runs_root, run_id)
    manifest_kwargs = {
        "run_id": run_id,
        "provider": provider.name,
        "config": config,
        "model_requested": effective_model,
        "endpoint": effective_endpoint,
        "params": effective_params,
        "n_reps": n_reps,
        "cases": cases,
        "notes": notes,
    }
    if "scope" in inspect.signature(recorder.write_manifest).parameters:
        manifest_kwargs["scope"] = scope
    # else: `scope` is a new recorder parameter (DESIGN.md §A) landing in a sibling change.
    # Until it does, the run is still recorded and every rep still carries `scope` itself.
    # Asking the signature rather than catching TypeError keeps a real TypeError raised
    # INSIDE write_manifest from being read as "the parameter is not there yet".
    recorder.write_manifest(run_directory, **manifest_kwargs)

    selected = cases if case_ids is None else [c for c in cases if c.id in set(case_ids)]
    if case_ids is not None and len(selected) != len(set(case_ids)):
        missing = set(case_ids) - {c.id for c in selected}
        raise ValueError(f"unknown case ids: {sorted(missing)}")
    # A native agent has no backend.py: its tools are the application's own.
    backend_factory = None if native else load_backend_factory(agent_dir)
    if native:
        config.runner_spec()  # validate the block before anything is executed
        if capture_session is not None and workers > 1:
            # Wire requests are attributed to a rep by the window it ran in, which is only
            # exact when one rep runs at a time.
            workers = 1

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
        # Before the episode, so a crossed ceiling costs nothing: no request is sent.
        if cost_ceiling is not None:
            cost_ceiling.check()
        start = time.monotonic()
        window_start = capture_session.now() if capture_session is not None else None
        if native:
            episode = run_native_episode(
                config,
                case,
                rep=rep,
                seed=seed_for(case.id, rep),
                model=effective_model,
                endpoint=effective_endpoint,
                options=runner_options or RunnerOptions(),
                agent_dir=agent_dir,
            )
        else:
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
        wire_requests = (
            capture_session.requests_between(window_start, capture_session.now())
            if capture_session is not None and window_start is not None
            else []
        )
        billing = _billing_message(episode.api_error)
        if billing:
            raise BillingError(
                f"provider reports a billing problem, run aborted before recording anything "
                f"misleading (rerun once the account is funded; completed reps are kept): "
                f"{billing}"
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
            episode_source=episode_source,
            scope=scope,
            continuation_used=bool(getattr(episode, "continuation_used", False)),
            wire_requests=wire_requests or list(getattr(episode, "wire_requests", ()) or ()),
            runner=dict(getattr(episode, "runner", {}) or {}),
        )
        recorder.write_rep(run_directory, record)
        if cost_ceiling is not None:
            cost_ceiling.observe(provider.name, effective_model, record.usage)
        return record

    if workers <= 1:
        for case, rep in todo:
            record = one(case, rep)
            if on_rep_done:
                on_rep_done(record)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, case, rep): (case.id, rep) for case, rep in todo}
            try:
                for future in as_completed(futures):
                    record = future.result()
                    if on_rep_done:
                        on_rep_done(record)
            except (BillingError, CostCeilingExceeded):
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    recorder.write_summary(run_directory)
    return run_directory
