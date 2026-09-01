"""Identity-linked Anthropic keys need an anthropic-workspace-id header on every request
(found live 2026-09-01: 400 "anthropic-workspace-id is required when authenticating with an
identity-linked API key"). ANTHROPIC_WORKSPACE_ID, when set, becomes a default header."""

from __future__ import annotations

import anthropic

from upshift.providers.anthropic_provider import AnthropicProvider


class _Capture:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_workspace_id_becomes_default_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    monkeypatch.setattr(anthropic, "Anthropic", _Capture)
    client = AnthropicProvider()._get_client()
    assert client.kwargs["default_headers"] == {"anthropic-workspace-id": "wrkspc_test"}


def test_no_workspace_id_means_no_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", _Capture)
    client = AnthropicProvider()._get_client()
    assert "default_headers" not in client.kwargs
