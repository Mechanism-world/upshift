"""Thin wrapper over the openai SDK. It never interprets the conversation: the agent loop
hands it a fully-built request body and it returns the verbatim response as a plain dict.

The client is created lazily so this module imports cleanly without OPENAI_API_KEY set; a
missing key surfaces as a ProviderAPIError at call time.

Flex tier (service_tier="flex"): same 50% token rate as the Batch API but synchronous, and
prompt caching stacks on top — the cheapest transport for our cache-friendly workload
(identical system+tools prefix on every call, append-only conversations). Capacity 429s on
flex are not charged and are retried by the SDK. Two cost aids are injected into every
request: `service_tier` (when flex) and a deterministic `prompt_cache_key` derived from the
request's static prefix, which improves cache-hit routing across concurrent episodes.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from upshift.providers.base import Provider, ProviderAPIError

TIMEOUT_S = 120.0
FLEX_TIMEOUT_S = 900.0  # flex is slower; the guide recommends a 15-minute timeout
MAX_RETRIES = 5


class OpenAIProvider(Provider):
    def __init__(self, service_tier: str | None = None) -> None:
        self.service_tier = service_tier
        self.name = "openai-flex" if service_tier == "flex" else "openai"
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ProviderAPIError(
                    message=(
                        "OPENAI_API_KEY is not set. Export it (or use provider 'sim' for a "
                        "cost-free deterministic run)."
                    ),
                    status_code=None,
                    error_type="missing_api_key",
                )
            from openai import OpenAI

            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": FLEX_TIMEOUT_S if self.service_tier == "flex" else TIMEOUT_S,
                "max_retries": MAX_RETRIES,
            }
            base_url = os.environ.get("OPENAI_BASE_URL")
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """seed_key and sim_context are ignored; real models are not seeded by us."""
        import openai

        client = self._get_client()
        request = dict(request)
        if self.service_tier:
            request.setdefault("service_tier", self.service_tier)
        request.setdefault("prompt_cache_key", _cache_key(endpoint, request))
        try:
            if endpoint == "chat_completions":
                result = client.chat.completions.create(**request)
            elif endpoint == "responses":
                result = client.responses.create(**request)
            else:
                raise ValueError(f"unknown endpoint {endpoint!r}")
        except openai.APIStatusError as exc:
            raise ProviderAPIError(
                message=_status_message(exc),
                status_code=getattr(exc, "status_code", None),
                error_type="api_status_error",
            ) from exc
        except openai.APIError as exc:
            raise ProviderAPIError(
                message=str(getattr(exc, "message", None) or exc) or exc.__class__.__name__,
                status_code=None,
                error_type="api_error",
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ProviderAPIError(
                message=f"{exc.__class__.__name__}: {exc}",
                status_code=None,
                error_type="network_error",
            ) from exc
        return result.model_dump(mode="json")


def _cache_key(endpoint: str, request: dict[str, Any]) -> str:
    """Deterministic routing hint for prompt caching: identical static prefixes (model +
    tools + system text) share a key, so concurrent episodes hit the same cache shard."""
    convo = request.get("messages") if endpoint == "chat_completions" else request.get("input")
    first = convo[0] if isinstance(convo, list) and convo else None
    blob = json.dumps([request.get("model"), request.get("tools"), first], sort_keys=True)
    return "upshift-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def _status_message(exc: Any) -> str:
    """Best-effort extraction of the API's own error message."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(body.get("message"), str):
            return body["message"]
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(exc)
