"""Secret redaction tests: API keys must never reach persistence or messages."""

from __future__ import annotations

import json

from endpoint_ledger.db.models import Observation
from endpoint_ledger.runner.request_builder import build_request_payload
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from endpoint_ledger.sanitize import redact_secrets, sanitize_message
from tests.helpers import make_endpoint, sync_probes


def test_sanitize_message_redacts_sk_patterns():
    message = "Auth failed with key sk-1234567890abcdefghijklm no"
    sanitized = sanitize_message(message)
    assert "sk-1234567890abcdefghijklm" not in sanitized
    assert "[REDACTED]" in sanitized


def test_sanitize_message_redacts_env_key_value(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-TESTKEYVALUE123456")
    message = "request failed with sk-TESTKEYVALUE123456 embedded"
    sanitized = sanitize_message(message)
    assert "sk-TESTKEYVALUE123456" not in sanitized
    assert "[REDACTED]" in sanitized


def test_redact_secrets_removes_authorization_and_api_key_fields():
    payload = {
        "headers": {"Authorization": "Bearer sk-abc123", "X-API-Key": "secret"},
        "body": {"messages": [{"role": "user", "content": "hi"}]},
    }
    redacted = redact_secrets(payload)
    assert redacted["headers"]["Authorization"] == "[REDACTED]"
    assert redacted["headers"]["X-API-Key"] == "[REDACTED]"
    assert redacted["body"] == payload["body"]


def test_build_request_payload_never_contains_credentials(db, settings):
    endpoint = make_endpoint(
        db,
        request_defaults={
            "request": {"api_key": "sk-LOOKATME", "headers": {"Authorization": "Bearer sk-bearer"}}
        },
    )
    probes = sync_probes(db)
    payload = build_request_payload(endpoint, probes[0])
    serialized = json.dumps(payload)
    assert "sk-LOOKATME" not in serialized
    assert "sk-bearer" not in serialized
    assert "[REDACTED]" in serialized


def test_stored_observations_contain_no_api_key(db, settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SUPERSECRETVALUE000")
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()

    runner = SuiteRunner(db, settings)
    runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=1, live=False))
    db.commit()

    for observation in db.query(Observation).all():
        assert "sk-SUPERSECRETVALUE000" not in json.dumps(observation.raw_request)
        assert "sk-SUPERSECRETVALUE000" not in json.dumps(observation.raw_response or {})
        if observation.error_message_sanitized:
            assert "sk-" not in observation.error_message_sanitized
