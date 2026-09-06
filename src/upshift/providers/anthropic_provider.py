"""Thin wrapper over the anthropic SDK for the Messages API. Like the OpenAI provider it
never interprets the conversation: the agent loop hands it a fully-built request body
(`endpoint="messages"`) and it returns the verbatim response as a plain dict.

The client is created lazily so this module imports cleanly without ANTHROPIC_API_KEY set; a
missing key surfaces as a ProviderAPIError at call time.

There is no flex/priority tier to inject and no batch provider in v0.3, so nothing is added
to the request: what the loop built is what is sent and what the record shows.
"""

from __future__ import annotations

import inspect
import os
import re
from functools import cache
from typing import Any

from upshift.providers.base import Provider, ProviderAPIError

MESSAGES = "messages"

#: The sampling parameters `anthropic` >= 1.1.0 removed from `Messages.create()`.
SAMPLING_PARAMS = ("temperature", "top_p", "top_k")

#: An SDK that refuses a request parameter raises a TypeError before anything is sent, so the
#: API never gets to answer. Recorded as the 400 the API returns for the same request, so the
#: differ's sampling-params signature — which keys on a 400 whose message names
#: temperature/top_p/top_k — still fires and its repair (drop the param) stays reachable.
#: For the sampling params themselves this is now a fallback: the loop routes them through
#: `extra_body` on an SDK that dropped them (see `messages_create_accepts`), so the request
#: reaches the wire and the API's own answer is what the run records. This mapping remains for
#: any OTHER parameter a future SDK removes. See DESIGN.md "Params mapping".
_RE_UNEXPECTED_KWARG = re.compile(r"unexpected keyword argument", re.IGNORECASE)
SDK_REJECTED_PARAM_STATUS = 400
TIMEOUT_S = 600.0  # thinking + long tool turns; the SDK also retries
MAX_RETRIES = 5


@cache
def messages_create_accepts(name: str) -> bool:
    """Does the INSTALLED SDK take `name` as a keyword of `Messages.create()`?

    `anthropic` >= 1.1.0 dropped `temperature`/`top_p`/`top_k` from that signature — no
    parameter and no `**kwargs` — so passing one raises TypeError in process and the request
    never leaves the machine. That makes an upgrade undecidable: the same in-process failure
    is recorded on BOTH models and the API, which is the thing being measured, never speaks.
    The loop asks this once per name and routes what the SDK will not take through
    `extra_body`, the SDK's own escape hatch, so the wire is reached and the API decides.
    On an older pinned SDK every name is still accepted and nothing moves.

    A signature that cannot be read is assumed to accept the name: that is the pre-1.1.0
    behaviour, and a wrong guess still surfaces as the TypeError mapped above.
    """
    try:
        from anthropic.resources.messages import Messages

        parameters = inspect.signature(Messages.create).parameters
    except Exception:  # noqa: BLE001 - an unreadable SDK must not break request building
        return True
    if name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ProviderAPIError(
                    message=(
                        "ANTHROPIC_API_KEY is not set. Export it (or use provider 'sim' for a "
                        "cost-free deterministic run)."
                    ),
                    status_code=None,
                    error_type="missing_api_key",
                )
            from anthropic import Anthropic

            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": TIMEOUT_S,
                "max_retries": MAX_RETRIES,
            }
            base_url = os.environ.get("ANTHROPIC_BASE_URL")
            if base_url:
                kwargs["base_url"] = base_url
            # Identity-linked API keys are rejected with 400 "anthropic-workspace-id is
            # required ..." unless every request names the workspace it acts in. The SDK
            # (1.3.0) has no client option for it, so it goes in as a default header.
            workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            if workspace_id:
                kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
            self._client = Anthropic(**kwargs)
        return self._client

    def call(
        self,
        endpoint: str,
        request: dict[str, Any],
        seed_key: str,
        sim_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """seed_key and sim_context are ignored; real models are not seeded by us."""
        import anthropic

        if endpoint != MESSAGES:
            raise ValueError(
                f"provider 'anthropic' only serves the {MESSAGES!r} endpoint, got {endpoint!r}"
            )
        client = self._get_client()
        try:
            result = client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            raise ProviderAPIError(
                message=_status_message(exc),
                status_code=getattr(exc, "status_code", None),
                error_type="api_status_error",
            ) from exc
        except anthropic.APIError as exc:
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
        except TypeError as exc:
            message = str(exc)
            if not _RE_UNEXPECTED_KWARG.search(message):
                raise  # not a parameter the SDK refuses to send; a real bug, let it surface
            raise ProviderAPIError(
                message=(
                    f"the installed anthropic SDK rejected a request parameter before it "
                    f"reached the wire: {message}"
                ),
                status_code=SDK_REJECTED_PARAM_STATUS,
                error_type="api_status_error",
            ) from exc
        return result.model_dump(mode="json")

    def preflight_models(self, ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """GET /v1/models/{id} for each id (free). -> {id: capabilities dict or None}.

        None means the model exists but reports no capabilities block; a model the key
        cannot see raises ProviderAPIError (404) like any other API failure.
        """
        import anthropic

        client = self._get_client()
        out: dict[str, dict[str, Any] | None] = {}
        for model_id in ids:
            try:
                model = client.models.retrieve(model_id)
            except anthropic.APIStatusError as exc:
                raise ProviderAPIError(
                    message=_status_message(exc),
                    status_code=getattr(exc, "status_code", None),
                    error_type="api_status_error",
                ) from exc
            except anthropic.APIError as exc:
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
            data = model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)
            capabilities = data.get("capabilities")
            out[model_id] = capabilities if isinstance(capabilities, dict) else None
        return out


def _status_message(exc: Any) -> str:
    """The API's own error message, verbatim, with best-effort fallbacks."""
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
