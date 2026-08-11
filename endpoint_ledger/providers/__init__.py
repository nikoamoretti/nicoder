"""Provider registry.

Adding a new provider (Anthropic, Google, ...) means implementing a
``ProviderAdapter`` and registering it here. The core runner and database model
do not change.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.providers.base import ProviderAdapter, ProviderUnavailableError
from endpoint_ledger.providers.mock import MockProvider
from endpoint_ledger.providers.openai_adapter import OpenAIAdapter
from endpoint_ledger.services.credentials import PROVIDER_OPENAI, resolve_credential

PROVIDER_NAMES = ("mock", "openai")


def get_provider(
    name: str,
    settings: Settings | None = None,
    session: Session | None = None,
) -> ProviderAdapter:
    """Build a provider adapter.

    Live providers receive a credential resolver bound to the shared
    application credential service (and the caller's session). Without a
    session a live adapter is built without a resolver and reports itself
    unavailable.
    """
    settings = settings or get_settings()
    if name == "mock":
        return MockProvider(
            latency_base_ms=settings.mock_latency_base_ms,
            latency_jitter_ms=settings.mock_latency_jitter_ms,
        )
    if name == "openai":
        resolver = (
            (lambda: resolve_credential(session, PROVIDER_OPENAI, settings)) if session is not None else None
        )
        return OpenAIAdapter(settings=settings, credential_resolver=resolver)
    raise KeyError(f"unknown provider {name!r}; available: {', '.join(PROVIDER_NAMES)}")


__all__ = [
    "PROVIDER_NAMES",
    "ProviderAdapter",
    "ProviderUnavailableError",
    "get_provider",
]