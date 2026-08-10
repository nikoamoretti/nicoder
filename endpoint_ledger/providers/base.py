"""Provider adapter abstraction.

A ``ProviderAdapter`` turns one endpoint configuration + probe version into a
provider-neutral :class:`CanonicalProviderResponse`. Provider-specific behavior
(raw responses, request IDs, token usage, error shapes) is deliberately
preserved by each adapter; we do not normalize it away through a gateway.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from endpoint_ledger.db.models import ProbeVersion, ProviderEndpoint
from endpoint_ledger.runner.canonical_response import CanonicalProviderResponse


class ProviderUnavailableError(RuntimeError):
    """Raised when an adapter cannot run at all (e.g. missing API key)."""


class ProviderAdapter(ABC):
    provider_name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this provider can currently be invoked."""

    @abstractmethod
    def invoke(
        self,
        endpoint_config: ProviderEndpoint,
        probe_version: ProbeVersion,
        repeat_index: int,
    ) -> CanonicalProviderResponse:
        """Invoke the endpoint and return a normalized response.

        A completed-but-failed request is returned as a response with
        ``error`` set (so the runner persists an Observation). Providers that
        are entirely unavailable raise :class:`ProviderUnavailableError`.
        """
