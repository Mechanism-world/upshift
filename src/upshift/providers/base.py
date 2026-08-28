"""Provider interface. Both the real OpenAI client and the local simulator implement this.

A provider takes a fully-built request dict for one of the two supported endpoints and
returns the verbatim response as a plain dict. It never interprets the conversation; the
agent loop owns message building and tool-call parsing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderAPIError(Exception):
    """An API-level failure (4xx/5xx). Carries enough to be recorded and matched on."""

    def __init__(self, message: str, status_code: int | None = None, error_type: str = "api_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type

    def to_dict(self) -> dict[str, Any]:
        return {"status_code": self.status_code, "message": self.message, "type": self.error_type}


class Provider(ABC):
    """name is recorded in every run manifest; verdicts may only cite provider='openai'."""

    name: str

    @abstractmethod
    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one API call.

        endpoint: "chat_completions" or "responses".
        request: verbatim request body (model, messages/input, tools, params).
        seed_key: stable string "<case_id>:<rep>:<call_idx>"; the sim uses it for
            deterministic behavior, the OpenAI provider ignores it.
        sim_context: {"case_id": str, "rep": int, "sim": <case.sim block>} — consumed only
            by the sim provider (oracle plan + vulnerability flags); the OpenAI provider
            ignores it entirely.
        Returns the response as a dict. Raises ProviderAPIError on API errors.
        """


def get_provider(name: str) -> Provider:
    if name == "openai":
        from upshift.providers.openai_provider import OpenAIProvider

        return OpenAIProvider()
    if name == "openai-batch":
        from upshift.providers.openai_batch import OpenAIBatchProvider

        return OpenAIBatchProvider()
    if name == "openai-flex":
        from upshift.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(service_tier="flex")
    if name == "sim":
        from upshift.providers.sim import SimProvider

        return SimProvider()
    raise ValueError(f"unknown provider {name!r}")
