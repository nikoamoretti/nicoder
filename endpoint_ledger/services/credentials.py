"""Credential storage and resolution — the single authority.

Two explicit sources are supported: the macOS Keychain (via ``keyring``) and
the ``OPENAI_API_KEY`` environment variable. An explicit active-source
selection persists in ``CredentialMetadata``; the default after saving a
Keychain credential is Keychain. There is **no silent fallback**: resolution
uses exactly the active source, and when both sources hold different
credentials without an explicit selection the resolver refuses (ambiguous).

The secret itself lives only in the Keychain / environment. PostgreSQL holds
only non-secret metadata: SHA-256 fingerprints, validation status, and
normalized (redacted) identity fields. The secret is never logged, never
returned to the browser, and never included in evidence exports.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import keyring
from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import CredentialMetadata, PreflightRecord
from endpoint_ledger.sanitize import sanitize_message

PROVIDER_OPENAI = "openai"
KEYCHAIN = "keychain"
ENVIRONMENT = "environment"
SOURCES = (KEYCHAIN, ENVIRONMENT)
_CREDENTIAL_KEY = "api-key"
_OPENAI_IDENTITY_URL = "https://api.openai.com/v1/me"

INVALIDATION_REASON = "CREDENTIAL CHANGED SINCE PREFLIGHT"

IdentityClient = Callable[[str, float], dict[str, Any]]


class CredentialResolutionError(RuntimeError):
    """Raised when no credential is resolvable under the active-source policy.

    ``reason`` is one of ``no_credential``, ``ambiguous``, ``not_found``.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedCredential:
    """A resolved credential: which source, its fingerprint, never the value."""

    source: str
    value: str
    fingerprint: str
    configured: bool = True


def _account(provider: str, settings: Settings) -> str:
    return f"{settings.credential_service_name}/{provider}/{_CREDENTIAL_KEY}"


def fingerprint_of(secret: str) -> str:
    """Non-secret SHA-256 fingerprint of a credential value."""
    return hashlib.sha256(f"endpoint-ledger-credential:v1:{secret}".encode()).hexdigest()


def source_value(provider: str, source: str, settings: Settings) -> str | None:
    """The secret value from a single explicit source, or None."""
    if source == KEYCHAIN:
        try:
            return keyring.get_password(_account(provider, settings), provider) or None
        except Exception:  # pragma: no cover - keyring backend unavailable
            return None
    if source == ENVIRONMENT:
        if provider == PROVIDER_OPENAI:
            return os.getenv("OPENAI_API_KEY") or None
        return None
    raise KeyError(f"unknown credential source {source!r}")


def source_fingerprint(provider: str, source: str, settings: Settings) -> str | None:
    value = source_value(provider, source, settings)
    return fingerprint_of(value) if value else None


def _metadata(session: Session, provider: str) -> CredentialMetadata:
    row = session.scalar(select(CredentialMetadata).where(CredentialMetadata.provider == provider))
    if row is None:
        row = CredentialMetadata(provider=provider)
        session.add(row)
        session.flush()
    return row


def _explicit_source(session: Session, provider: str) -> str | None:
    return _metadata(session, provider).active_source


def active_source(session: Session, provider: str, settings: Settings | None = None) -> str | None:
    """The effective active source: explicit selection, else derived defaults."""
    settings = settings or get_settings()
    explicit = _explicit_source(session, provider)
    if explicit is not None:
        return explicit
    if source_value(provider, KEYCHAIN, settings) is not None:
        return KEYCHAIN
    if source_value(provider, ENVIRONMENT, settings) is not None:
        return ENVIRONMENT
    return None


def resolve_credential(
    session: Session,
    provider: str,
    settings: Settings | None = None,
) -> ResolvedCredential:
    """Resolve the active credential under the explicit active-source policy.

    Raises ``CredentialResolutionError`` when nothing is configured, or when
    both sources hold different credentials with no explicit selection
    (ambiguous — never a silent fallback).
    """
    settings = settings or get_settings()
    explicit = _explicit_source(session, provider)

    if explicit is not None:
        value = source_value(provider, explicit, settings)
        if value is None:
            raise CredentialResolutionError(
                f"no credential available in active source '{explicit}'",
                "no_credential",
            )
        return ResolvedCredential(source=explicit, value=value, fingerprint=fingerprint_of(value))

    keychain_value = source_value(provider, KEYCHAIN, settings)
    env_value = source_value(provider, ENVIRONMENT, settings) if provider == PROVIDER_OPENAI else None
    if keychain_value and env_value:
        raise CredentialResolutionError(
            "both keychain and environment credentials are configured and no active "
            "source is selected; select one explicitly",
            "ambiguous",
        )
    if keychain_value:
        return ResolvedCredential(source=KEYCHAIN, value=keychain_value, fingerprint=fingerprint_of(keychain_value))
    if env_value:
        return ResolvedCredential(source=ENVIRONMENT, value=env_value, fingerprint=fingerprint_of(env_value))
    raise CredentialResolutionError(
        "no provider credential is configured (keychain or environment)",
        "no_credential",
    )


def active_fingerprint(session: Session, provider: str, settings: Settings | None = None) -> str | None:
    try:
        return resolve_credential(session, provider, settings).fingerprint
    except CredentialResolutionError:
        return None


def credential_status(session: Session, provider: str, settings: Settings | None = None) -> dict[str, Any]:
    """Full non-secret status used by the dashboard. Never returns the secret."""
    settings = settings or get_settings()
    metadata = _metadata(session, provider)
    keychain_value = source_value(provider, KEYCHAIN, settings)
    env_value = source_value(provider, ENVIRONMENT, settings) if provider == PROVIDER_OPENAI else None
    keychain_fp = fingerprint_of(keychain_value) if keychain_value else None
    env_fp = fingerprint_of(env_value) if env_value else None

    explicit = metadata.active_source
    effective = active_source(session, provider, settings)
    both = bool(keychain_value and env_value)
    ambiguous = both and explicit is None and (keychain_fp != env_fp)
    if both and explicit is None and keychain_fp == env_fp:
        effective = KEYCHAIN

    latest_preflight = session.scalar(
        select(PreflightRecord).where(PreflightRecord.provider == provider).order_by(PreflightRecord.id.desc())
    )
    binding: dict[str, Any] = {"ok": False, "reason": "no preflight on record"}
    if latest_preflight is not None:
        binding = {
            "ok": (
                bool(latest_preflight.ready)
                and latest_preflight.binding_valid is not False
                and effective is not None
                and latest_preflight.credential_fingerprint == active_fingerprint(session, provider, settings)
            ),
            "reason": (
                "CREDENTIAL CHANGED SINCE PREFLIGHT"
                if latest_preflight.binding_valid is False
                else (
                    "preflight not ready"
                    if not latest_preflight.ready
                    else "preflight bound to a different credential fingerprint"
                )
            ),
        }

    return {
        "provider": provider,
        "configured": bool(keychain_value or env_value),
        "keychain": {"configured": bool(keychain_value), "fingerprint": keychain_fp},
        "environment": {"configured": bool(env_value), "fingerprint": env_fp},
        "active_source": effective,
        "explicit_source": explicit,
        "active_fingerprint": active_fingerprint(session, provider, settings),
        "ambiguous": ambiguous,
        "storage_method": metadata.storage_method or ("keychain" if keychain_value else "environment"),
        "last_updated_at": metadata.last_updated_at,
        "last_validated_at": metadata.last_validated_at,
        "validation_status": metadata.validation_status,
        "validation_message": metadata.validation_message,
        "identity_user_id": metadata.identity_user_id,
        "identity_email_redacted": metadata.identity_email_redacted,
        "identity_orgs": metadata.identity_orgs or [],
        "preflight_binding": binding,
    }


def store_credential(
    session: Session,
    provider: str,
    secret_value: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store a credential in the Keychain, record metadata only, and select it."""
    if not secret_value or not secret_value.strip():
        raise ValueError("credential value may not be empty")
    settings = settings or get_settings()
    secret = secret_value.strip()
    keyring.set_password(_account(provider, settings), provider, secret)
    metadata = _metadata(session, provider)
    now = datetime.now(UTC)
    metadata.storage_method = KEYCHAIN
    metadata.configured = True
    metadata.last_updated_at = now
    metadata.keychain_fingerprint = fingerprint_of(secret)
    if provider == PROVIDER_OPENAI:
        env_value = os.getenv("OPENAI_API_KEY")
        metadata.environment_fingerprint = fingerprint_of(env_value) if env_value else None
    metadata.active_source = KEYCHAIN
    invalidate_preflight_binding(session, provider)
    session.commit()
    return credential_status(session, provider, settings)


def remove_credential(session: Session, provider: str, settings: Settings | None = None) -> dict[str, Any]:
    """Remove the Keychain credential and clear the active-source selection."""
    settings = settings or get_settings()
    try:
        keyring.delete_password(_account(provider, settings), provider)
    except Exception:  # pragma: no cover - backend raises on missing items
        pass
    metadata = _metadata(session, provider)
    metadata.configured = False  # keychain storage no longer holds a credential
    metadata.last_updated_at = datetime.now(UTC)
    metadata.keychain_fingerprint = None
    if metadata.active_source == KEYCHAIN:
        metadata.active_source = None  # never silently fall back to the environment
    invalidate_preflight_binding(session, provider)
    session.commit()
    return credential_status(session, provider, settings)


def set_active_source(session: Session, provider: str, source: str, settings: Settings | None = None) -> dict[str, Any]:
    """Explicitly select the active credential source (no fallback)."""
    if source not in SOURCES:
        raise KeyError(f"unknown credential source {source!r}")
    settings = settings or get_settings()
    if source_value(provider, source, settings) is None:
        raise CredentialResolutionError(f"no credential configured in source '{source}'", "no_credential")
    metadata = _metadata(session, provider)
    metadata.active_source = source
    invalidate_preflight_binding(session, provider)
    session.commit()
    return credential_status(session, provider, settings)


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    local, _, domain = email.partition("@")
    if not domain:
        return f"{local[0]}***" if local else "***"
    return f"{local[0]}***@{domain}" if local else f"***@{domain}"


def _normalize_identity(payload: dict[str, Any]) -> dict[str, Any]:
    orgs = payload.get("orgs") or []
    return {
        "user_id": str(payload.get("id") or ""),
        "email": payload.get("email"),
        "orgs": [{"id": str(o.get("id") or ""), "title": str(o.get("title") or "")} for o in orgs],
    }


def _identity_request(api_key: str, timeout: float) -> dict[str, Any]:
    import httpx

    response = httpx.get(
        _OPENAI_IDENTITY_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def validate_credential(
    session: Session,
    provider: str,
    settings: Settings | None = None,
    *,
    identity_client: IdentityClient | None = None,
) -> dict[str, Any]:
    """Validate the active credential against the provider identity endpoint.

    Uses ``GET /v1/me`` (an identity call, not a model request) and stores only
    the validation status, a non-secret fingerprint, and normalized redacted
    identity metadata. Success proves identity, not paid quota.
    """
    settings = settings or get_settings()
    metadata = _metadata(session, provider)
    metadata.last_validated_at = datetime.now(UTC)
    try:
        credential = resolve_credential(session, provider, settings)
    except CredentialResolutionError as exc:
        metadata.validation_status = "invalid"
        metadata.validation_message = sanitize_message(str(exc))
        session.commit()
        return {"ok": False, "status": "invalid", "message": metadata.validation_message}

    client = identity_client or _identity_request
    try:
        identity = _normalize_identity(client(credential.value, settings.openai_timeout_seconds))
    except Exception as exc:  # network/auth/timeout — never leak key material
        metadata.validation_status = "invalid"
        metadata.validation_message = sanitize_message(
            f"identity endpoint refused validation: {type(exc).__name__}"
        )
        session.commit()
        return {"ok": False, "status": "invalid", "message": metadata.validation_message}

    metadata.validation_status = "valid"
    metadata.validation_message = (
        "Identity valid. A new live qualification smoke is still required to verify "
        "model access and billing availability."
    )
    metadata.identity_user_id = identity["user_id"] or None
    metadata.identity_email_redacted = _redact_email(identity["email"])
    metadata.identity_orgs = identity["orgs"]
    session.commit()
    return {
        "ok": True,
        "status": "valid",
        "message": metadata.validation_message,
        "identity": {
            "user_id": metadata.identity_user_id,
            "email_redacted": metadata.identity_email_redacted,
            "orgs": metadata.identity_orgs,
            "active_source": metadata.active_source or active_source(session, provider, settings),
            "fingerprint": active_fingerprint(session, provider, settings),
        },
    }


def record_preflight_binding(
    session: Session,
    provider: str,
    record: PreflightRecord,
    settings: Settings | None = None,
) -> None:
    """Bind a fresh preflight record to the resolved active credential."""
    settings = settings or get_settings()
    try:
        credential = resolve_credential(session, provider, settings)
    except CredentialResolutionError:
        record.credential_fingerprint = None
        record.source_binding = None
        record.binding_valid = False
        record.binding_reason = "no resolvable credential at preflight time"
        return
    record.credential_fingerprint = credential.fingerprint
    record.source_binding = credential.source
    record.binding_valid = True
    record.binding_reason = None


def invalidate_preflight_binding(
    session: Session,
    provider: str,
    reason: str = INVALIDATION_REASON,
) -> None:
    """Mark the latest preflight invalid: a live run is blocked until re-run."""
    record = session.scalar(
        select(PreflightRecord).where(PreflightRecord.provider == provider).order_by(PreflightRecord.id.desc())
    )
    if record is None:
        return
    record.binding_valid = False
    record.binding_reason = sanitize_message(reason)
    session.add(record)