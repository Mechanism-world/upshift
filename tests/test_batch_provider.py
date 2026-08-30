"""Tests for the OpenAI Batch provider's turn-wave coordinator and the runner's
lifecycle-hook integration. All batch traffic goes through a fake client."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from upshift.providers.base import Provider, ProviderAPIError
from upshift.providers.openai_batch import OpenAIBatchProvider

VICTIM = Path(__file__).resolve().parent.parent / "victim" / "booking_agent"


class FakeBatchClient:
    """Mimics the sliver of the openai SDK the batch provider touches. Each created batch
    is completed on first retrieve; per-request responses come from `responder`."""

    def __init__(self, responder, batch_status="completed"):
        self._responder = responder
        self._batch_status = batch_status
        self._files: dict[str, bytes] = {}
        self._outputs: dict[str, str] = {}
        self.created_batches: list[list[dict]] = []
        self.files = SimpleNamespace(create=self._file_create, content=self._file_content)
        self.batches = SimpleNamespace(
            create=self._batch_create, retrieve=self._batch_retrieve, cancel=lambda bid: None
        )

    def _file_create(self, file, purpose):
        _name, buf = file
        fid = f"file-{len(self._files)}"
        self._files[fid] = buf.read()
        return SimpleNamespace(id=fid)

    def _file_content(self, fid):
        return SimpleNamespace(text=self._outputs[fid])

    def _batch_create(self, input_file_id, endpoint, completion_window):
        requests = [
            json.loads(line)
            for line in self._files[input_file_id].decode().splitlines()
            if line.strip()
        ]
        self.created_batches.append(requests)
        out_lines = [json.dumps(self._responder(r)) for r in requests]
        out_id = f"out-{len(self._outputs)}"
        self._outputs[out_id] = "\n".join(out_lines) + "\n"
        self._pending = SimpleNamespace(
            id=f"batch-{len(self.created_batches)}",
            status="in_progress",
            output_file_id=out_id,
            error_file_id=None,
            errors=None,
        )
        return self._pending

    def _batch_retrieve(self, bid):
        done = SimpleNamespace(**vars(self._pending))
        done.status = self._batch_status
        return done


def ok_line(request, body=None):
    return {
        "custom_id": request["custom_id"],
        "response": {
            "status_code": 200,
            "body": body or {"echo": request["custom_id"], "model": "m"},
        },
        "error": None,
    }


def make_provider(responder, **kwargs):
    return OpenAIBatchProvider(
        poll_interval=0.01, client=FakeBatchClient(responder, **kwargs)
    )


def run_episode_threads(provider, plan: dict[str, int], endpoint="chat_completions"):
    """plan: episode_key -> number of calls. Returns {episode_key: [result-or-exception]}."""
    provider.episodes_starting(list(plan))
    results: dict[str, list] = {k: [] for k in plan}

    def episode(key, calls):
        try:
            for idx in range(calls):
                try:
                    results[key].append(
                        provider.call(endpoint, {"model": "m", "who": key}, f"{key}:{idx}")
                    )
                except ProviderAPIError as e:
                    results[key].append(e)
                    return
        finally:
            provider.episode_finished(key)

    threads = [threading.Thread(target=episode, args=(k, n)) for k, n in plan.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "episode thread hung — wave coordinator deadlock"
    return results


def test_waves_advance_all_episodes_in_lockstep():
    provider = make_provider(ok_line)
    results = run_episode_threads(provider, {"a:1": 2, "b:1": 2, "c:1": 2})
    client = provider._client
    assert [len(b) for b in client.created_batches] == [3, 3]
    for key, res in results.items():
        assert [r["echo"] for r in res] == [f"{key}:0", f"{key}:1"]


def test_waves_shrink_as_episodes_finish():
    provider = make_provider(ok_line)
    run_episode_threads(provider, {"a:1": 1, "b:1": 3})
    assert [len(b) for b in provider._client.created_batches] == [2, 1, 1]


def test_per_request_error_hits_only_that_episode():
    def responder(request):
        if request["custom_id"].startswith("bad"):
            return {
                "custom_id": request["custom_id"],
                "response": {
                    "status_code": 400,
                    "body": {"error": {"message": "tools with reasoning_effort rejected"}},
                },
                "error": None,
            }
        return ok_line(request)

    results = run_episode_threads(make_provider(responder), {"bad:1": 1, "good:1": 1})
    err = results["bad:1"][0]
    assert isinstance(err, ProviderAPIError)
    assert err.status_code == 400
    assert "reasoning_effort" in err.message
    assert results["good:1"][0]["echo"] == "good:1:0"


def test_batch_level_failure_fails_the_wave():
    results = run_episode_threads(
        make_provider(ok_line, batch_status="failed"), {"a:1": 1, "b:1": 1}
    )
    for res in results.values():
        assert isinstance(res[0], ProviderAPIError)
        assert res[0].error_type == "batch_error"


def test_missing_result_line_is_an_error():
    def responder(request):
        line = ok_line(request)
        # drop b's result entirely
        return line if request["custom_id"].startswith("a") else {"custom_id": "orphan",
                                                                  "response": None,
                                                                  "error": {"message": "x"}}

    results = run_episode_threads(make_provider(responder), {"a:1": 1, "b:1": 1})
    assert results["a:1"][0]["echo"] == "a:1:0"
    assert isinstance(results["b:1"][0], ProviderAPIError)


def test_unregistered_episode_is_rejected():
    provider = make_provider(ok_line)
    with pytest.raises(ProviderAPIError, match="not registered"):
        provider.call("chat_completions", {"model": "m"}, "ghost:1:0")


def test_requests_are_byte_identical_to_sync_shape():
    provider = make_provider(ok_line)
    run_episode_threads(provider, {"a:1": 1})
    (batch,) = provider._client.created_batches
    (line,) = batch
    assert line == {
        "custom_id": "a:1:0",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": "m", "who": "a:1"},
    }


class RecordingHookProvider(Provider):
    """Sync provider with batching hooks, to test the runner's lifecycle integration."""

    name = "openai-batch"  # pretend, so records look like a batch run
    requires_all_workers = True

    def __init__(self):
        self.events: list[tuple[str, object]] = []
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def episodes_starting(self, keys):
        with self._lock:
            self.events.append(("start", sorted(keys)))

    def episode_finished(self, key):
        with self._lock:
            self.events.append(("finish", key))

    def call(self, endpoint, request, seed_key, sim_context=None):
        with self._lock:
            self.calls.append(seed_key)
        return {
            "id": "x",
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }


def test_runner_registers_all_episodes_before_any_call(tmp_path):
    from upshift.runner import run_suite

    provider = RecordingHookProvider()
    case_ids = ["happy_search_basic", "edge_out_of_scope_hotel"]
    run_suite(
        VICTIM,
        provider,
        "hooks-test",
        n_reps=2,
        runs_root=tmp_path,
        case_ids=case_ids,
        workers=1,  # runner must escalate to len(todo) itself
    )
    starts = [e for e in provider.events if e[0] == "start"]
    assert len(starts) == 1
    expected_keys = sorted(f"{cid}:{rep}" for cid in case_ids for rep in (1, 2))
    assert starts[0][1] == expected_keys
    # registration happened before any provider call
    assert provider.events[0][0] == "start"
    assert len(provider.calls) >= 4
    finishes = sorted(key for kind, key in provider.events if kind == "finish")
    assert finishes == expected_keys
