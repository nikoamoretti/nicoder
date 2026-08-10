"""Provider registry.

Adding a new provider (Anthropic, Google, ...) means implementing a
``ProviderAdapter`` and registering it here. The core runner and database model
do not change.
"""

from __future__ import annotations

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.providers.base import ProviderAdapter, ProviderUnavailableError
from endpoint_ledger.providers.mock import MockProvider
from endpoint_ledger.providers.openai_adapter import OpenAIAdapter

PROVIDER_NAMES = ("mock", "openai")


def get_provider(name: str, settings: Settings | None = None) -> ProviderAdapter:
    settings = settings or get_settings()
    if name == "mock":
        return MockProvider(
            latency_base_ms=settings.mock_latency_base_ms,
            latency_jitter_ms=settings.mock_latency_jitter_ms,
        )
    if name == "openai":
        return OpenAIAdapter(settings=settings)
    raise KeyError(f"unknown provider {name!r}; available: {', '.join(PROVIDER_NAMES)}")


__all__ = [
    "PROVIDER_NAMES",
    "ProviderAdapter",
    "ProviderUnavailableError",
    "get_provider",
]
