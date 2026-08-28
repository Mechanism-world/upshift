"""Thin wrapper over the openai SDK. It never interprets the conversation: the agent loop
hands it a fully-built request body and it returns the verbatim response as a plain dict.

The client is created lazily so this module imports cleanly without OPENAI_API_KEY set; a
missing key surfaces as a ProviderAPIError at call time.
"""

from __future__ import annotations

import os
from typing import Any

from upshift.providers.base import Provider, ProviderAPIError

TIMEOUT_S = 120.0
MAX_RETRIES = 5


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self) -> None:
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
                "timeout": TIMEOUT_S,
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
