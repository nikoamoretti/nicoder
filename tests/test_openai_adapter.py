"""OpenAI adapter tests using recorded synthetic fixtures, never live calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from endpoint_ledger.config import Settings
from endpoint_ledger.providers.base import ProviderUnavailableError
from endpoint_ledger.providers.openai_adapter import (
    OpenAIAdapter,
    compute_backoff_seconds,
    normalize_openai_response,
    should_retry,
)
from tests.helpers import make_endpoint, sync_probes

FIXTURES = Path(__file__).resolve().parent.parent / "probes" / "fixtures" / "openai"


def _load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _adapter(responses: list[Any], max_retries: int = 3) -> OpenAIAdapter:
    sleeps: list[float] = []

    def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)

    class FakeCompletions:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs: Any) -> Any:
            item = responses[min(self.calls, len(responses) - 1)]
            self.calls += 1
            if isinstance(item, Exception):
                raise item
            return SimpleNamespace(model_dump=lambda: item)

    def factory(api_key: str, timeout: float) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    adapter = OpenAIAdapter(
        settings=Settings(database_url="unused", openai_max_retries=max_retries),
        sleep_fn=sleep_fn,
        client_factory=factory,
    )
    adapter._sleeps = sleeps  # type: ignore[attr-defined]
    return adapter


@pytest.fixture()
def probe_version(db):
    sync_probes(db)
    return next(pv for pv in sync_probes(db) if pv.probe.stable_key == "structured_output_customer_object")


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.test/v1/chat/completions")


def _status_error(cls, code: int) -> Exception:
    response = httpx.Response(code, request=_req())
    return cls("boom", response=response, body=None)


def test_normalize_text_completion_fixture():
    raw = _load_fixture("text_completion.json")
    started = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
    completed = datetime(2026, 8, 9, 12, 0, 1, tzinfo=UTC)
    response = normalize_openai_response(raw, "gpt-4o-mini", started, completed)
    assert response.provider == "openai"
    assert response.model_requested == "gpt-4o-mini"
    assert response.model_reported == "gpt-4o-mini-2024-07-18"
    assert response.provider_request_id == "chatcmpl-8fRecordedText123"
    assert response.finish_reason == "stop"
    assert response.http_status == 200
    assert response.latency_ms == 1000
    assert response.usage.input_tokens == 82
    assert response.usage.output_tokens == 29
    assert response.text is not None and '"C-1042"' in response.text
    assert response.raw_response == raw


def test_normalize_tool_calls_fixture():
    raw = _load_fixture("tool_calls.json")
    started = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
    response = normalize_openai_response(raw, "gpt-4o-mini", started, started)
    assert [c.name for c in response.tool_calls] == ["lookup_order", "cancel_subscription"]
    assert response.tool_calls[0].arguments_dict == {"order_id": "ORD-8821"}
    assert response.tool_calls[1].arguments_dict == {"subscription_id": "S-3301"}
    assert response.finish_reason == "tool_calls"


def test_invoke_returns_normalized_response(probe_version, db):
    raw = _load_fixture("text_completion.json")
    adapter = _adapter([raw])
    endpoint = make_endpoint(db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini")
    response = adapter.invoke(endpoint, probe_version, 1)
    assert response.error is None
    assert response.provider == "openai"
    assert response.text is not None


def test_invoke_retries_transient_then_succeeds(probe_version, db, monkeypatch):
    raw = _load_fixture("text_completion.json")
    transient = _status_error(InternalServerError, 500)
    adapter = _adapter([transient, raw], max_retries=3)
    endpoint = make_endpoint(db, provider="openai", model_requested="gpt-4o-mini")
    response = adapter.invoke(endpoint, probe_version, 1)
    assert response.error is None
    assert len(adapter._sleeps) == 1


def test_invoke_does_not_retry_validation_failure(probe_version, db):
    bad_request = _status_error(BadRequestError, 400)
    adapter = _adapter([bad_request], max_retries=3)
    endpoint = make_endpoint(db, provider="openai", model_requested="gpt-4o-mini")
    response = adapter.invoke(endpoint, probe_version, 1)
    assert response.error is not None
    assert response.error.error_type == "BadRequestError"
    assert response.error.http_status == 400
    assert adapter._sleeps == []


def test_invoke_gives_up_after_max_retries(probe_version, db):
    transient = _status_error(InternalServerError, 500)
    adapter = _adapter([transient], max_retries=2)
    endpoint = make_endpoint(db, provider="openai", model_requested="gpt-4o-mini")
    response = adapter.invoke(endpoint, probe_version, 1)
    assert response.error is not None
    assert len(adapter._sleeps) == 2


def test_unavailable_without_api_key(probe_version, db, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    adapter = OpenAIAdapter(settings=Settings(database_url="unused"))
    assert adapter.is_available() is False
    endpoint = make_endpoint(db, provider="openai", model_requested="gpt-4o-mini")
    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        adapter.invoke(endpoint, probe_version, 1)


def test_build_payload_includes_tools_and_params(probe_version, db):
    endpoint = make_endpoint(db, provider="openai", model_requested="gpt-4o-mini")
    tool_probe = next(
        pv for pv in sync_probes(db) if pv.probe.stable_key == "tool_calling_lookup_order"
    )
    payload = OpenAIAdapter._build_payload(endpoint, tool_probe)
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"][0]["role"] == "system"
    assert "tools" in payload
    assert payload["tool_choice"] == "auto"
    assert payload["temperature"] == 0
    assert "api_key" not in json.dumps(payload).lower()


def test_should_retry_classification():
    assert should_retry(_status_error(InternalServerError, 500)) is True
    assert should_retry(_status_error(RateLimitError, 429)) is True
    assert should_retry(_status_error(BadRequestError, 400)) is False
    assert should_retry(_status_error(AuthenticationError, 401)) is False
    assert should_retry(APITimeoutError(request=_req())) is True
    assert should_retry(APIConnectionError(message="conn", request=_req())) is True


def test_compute_backoff_is_bounded():
    for attempt in range(1, 8):
        delay = compute_backoff_seconds(attempt)
        assert 0 < delay <= 30.0
    assert compute_backoff_seconds(1) <= compute_backoff_seconds(8)


def test_prose_fixture_normalizes_text():
    raw = _load_fixture("prose_response.json")
    started = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
    response = normalize_openai_response(raw, "gpt-4o-mini", started, started)
    assert "cannot cancel" in response.text
