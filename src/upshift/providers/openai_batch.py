"""OpenAI Batch API provider: turn-wave batching for agentic episodes at 50% token cost.

An interactive tool loop cannot be a single batch request, but a fleet of episodes can
advance in lockstep: every live episode parks its next request, one Batch API job executes
the whole wave, tool calls run locally, and the fleet advances to the next wave. Episodes
finish at different turn counts, so waves shrink as the run drains.

Contract with the runner: `requires_all_workers` makes the runner give every episode its own
thread and pre-register all episodes via `episodes_starting()` before any starts, so the
coordinator knows exactly when a wave is complete (every live episode has parked). The
runner must call `episode_finished()` in a finally block — a vanished episode would
otherwise stall the wave forever.

Batching changes transport and price, never behavior: request bodies are byte-identical to
the sync provider's, and results flow into the same records.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from typing import Any

from upshift.providers.base import Provider, ProviderAPIError

_ENDPOINT_URLS = {"chat_completions": "/v1/chat/completions", "responses": "/v1/responses"}
_TERMINAL = {"completed", "failed", "expired", "cancelled"}


class _Slot:
    __slots__ = ("endpoint", "error", "event", "request", "response", "seed_key")

    def __init__(self, endpoint: str, request: dict[str, Any], seed_key: str):
        self.endpoint = endpoint
        self.request = request
        self.seed_key = seed_key  # batch custom_id: "<case_id>:<rep>:<call_idx>"
        self.response: dict[str, Any] | None = None
        self.error: ProviderAPIError | None = None
        self.event = threading.Event()


class OpenAIBatchProvider(Provider):
    name = "openai-batch"
    requires_all_workers = True

    def __init__(self, poll_interval: float | None = None, client: Any | None = None):
        self._client = client
        self._poll_interval = poll_interval or float(os.environ.get("UPSHIFT_BATCH_POLL_S", "15"))
        self._wave_timeout = float(os.environ.get("UPSHIFT_BATCH_TIMEOUT_S", str(24 * 3600)))
        self._cond = threading.Condition()
        self._live: set[str] = set()
        self._parked: dict[str, _Slot] = {}
        self._wave = 0
        self._coordinator: threading.Thread | None = None

    # -- lifecycle hooks called by the runner ------------------------------------------

    def episodes_starting(self, keys: list[str]) -> None:
        with self._cond:
            self._live.update(keys)
            self._cond.notify_all()

    def episode_finished(self, key: str) -> None:
        with self._cond:
            self._live.discard(key)
            self._cond.notify_all()

    # -- Provider interface ------------------------------------------------------------

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if endpoint not in _ENDPOINT_URLS:
            raise ProviderAPIError(f"unknown endpoint {endpoint!r}")
        episode_key = seed_key.rsplit(":", 1)[0]  # "<case_id>:<rep>"
        slot = _Slot(endpoint, request, seed_key)
        with self._cond:
            if episode_key not in self._live:
                raise ProviderAPIError(
                    f"episode {episode_key} was not registered via episodes_starting(); "
                    "the batch provider requires the runner's lifecycle hooks"
                )
            self._parked[episode_key] = slot
            self._ensure_coordinator()
            self._cond.notify_all()
        slot.event.wait()
        if slot.error is not None:
            raise slot.error
        assert slot.response is not None
        return slot.response

    # -- coordinator -------------------------------------------------------------------

    def _ensure_coordinator(self) -> None:
        if self._coordinator is None or not self._coordinator.is_alive():
            self._coordinator = threading.Thread(
                target=self._run, name="upshift-batch-coordinator", daemon=True
            )
            self._coordinator.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                # A wave is ready when every live episode has parked its next request.
                while not (self._live and self._live <= set(self._parked)):
                    self._cond.wait()
                wave_slots = {k: self._parked.pop(k) for k in sorted(self._live)}
                self._wave += 1
                wave_no = self._wave
            try:
                self._execute_wave(wave_no, wave_slots)
            except ProviderAPIError as e:
                for slot in wave_slots.values():
                    slot.error = e
                    slot.event.set()
            # Deliberate blanket catch: if the coordinator thread died, every parked
            # episode would block forever. Any failure becomes a per-rep recorded error.
            except Exception as e:  # noqa: BLE001
                err = ProviderAPIError(f"batch wave failed: {e}", error_type="batch_error")
                for slot in wave_slots.values():
                    slot.error = err
                    slot.event.set()

    def _execute_wave(self, wave_no: int, wave_slots: dict[str, _Slot]) -> None:
        by_url: dict[str, dict[str, _Slot]] = {}
        for key, slot in wave_slots.items():
            by_url.setdefault(_ENDPOINT_URLS[slot.endpoint], {})[key] = slot
        for url, slots in by_url.items():
            results = self._submit_and_collect(wave_no, url, slots)
            for slot in slots.values():
                if slot.seed_key in results:
                    slot.response, slot.error = results[slot.seed_key]
                else:
                    slot.error = ProviderAPIError(
                        f"batch result missing for {slot.seed_key}", error_type="batch_error"
                    )
                slot.event.set()

    def _submit_and_collect(
        self, wave_no: int, url: str, slots: dict[str, _Slot]
    ) -> dict[str, tuple[dict | None, ProviderAPIError | None]]:
        client = self._ensure_client()
        lines = [
            json.dumps(
                {"custom_id": slot.seed_key, "method": "POST", "url": url, "body": slot.request}
            )
            for slot in slots.values()
        ]
        payload = ("\n".join(lines) + "\n").encode()
        input_file = client.files.create(
            file=(f"upshift-wave-{wave_no}.jsonl", io.BytesIO(payload)), purpose="batch"
        )
        batch = client.batches.create(
            input_file_id=input_file.id, endpoint=url, completion_window="24h"
        )
        print(
            f"[batch] wave {wave_no}: {len(slots)} request(s) via {url} -> {batch.id}",
            flush=True,
        )
        started = time.monotonic()
        while batch.status not in _TERMINAL:
            if time.monotonic() - started > self._wave_timeout:
                try:
                    client.batches.cancel(batch.id)
                finally:
                    raise ProviderAPIError(
                        f"batch {batch.id} timed out after {self._wave_timeout}s",
                        error_type="batch_timeout",
                    )
            time.sleep(self._poll_interval)
            batch = client.batches.retrieve(batch.id)
        if batch.status != "completed":
            errs = getattr(batch, "errors", None)
            raise ProviderAPIError(
                f"batch {batch.id} ended {batch.status}: {errs}", error_type="batch_error"
            )
        print(
            f"[batch] wave {wave_no}: completed in {time.monotonic() - started:.0f}s",
            flush=True,
        )

        results: dict[str, tuple[dict | None, ProviderAPIError | None]] = {}
        for file_id in (batch.output_file_id, batch.error_file_id):
            if not file_id:
                continue
            for line in client.files.content(file_id).text.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                results[item["custom_id"]] = self._parse_result_line(item)
        return results

    @staticmethod
    def _parse_result_line(item: dict) -> tuple[dict | None, ProviderAPIError | None]:
        response = item.get("response") or {}
        status = response.get("status_code")
        body = response.get("body") or {}
        if item.get("error"):
            err = item["error"]
            return None, ProviderAPIError(
                str(err.get("message", err)), error_type="api_status_error"
            )
        if status == 200:
            return body, None
        message = (body.get("error") or {}).get("message", f"HTTP {status}")
        return None, ProviderAPIError(message, status_code=status, error_type="api_status_error")

    def _ensure_client(self):
        if self._client is None:
            import openai

            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ProviderAPIError(
                    "OPENAI_API_KEY is not set (put it in .env or the environment)"
                )
            self._client = openai.OpenAI(
                api_key=api_key,
                base_url=os.environ.get("OPENAI_BASE_URL") or None,
                timeout=120.0,
                max_retries=5,
            )
        return self._client
