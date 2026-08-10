"""Credential storage and resolution.

Secrets are stored only in the operating system's secure credential store
(macOS Keychain via keyring). The database holds only non-secret metadata.
The application never reads secrets from files, notes, shell history, or
unrelated directories.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import keyring
from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import CredentialMetadata
from endpoint_ledger.sanitize import sanitize_message

PROVIDER_OPENAI = "openai"
_CREDENTIAL_KEY = "api-key"


def _account(provider: str, settings: Settings) -> str:
    return f"{settings.credential_service_name}/{provider}/{_CREDENTIAL_KEY}"


def resolve_api_key(provider: str, settings: Settings | None = None) -> str | None:
    """Resolve a provider credential: OS credential store, else the environment."""
    settings = settings or get_settings()
    try:
        key = keyring.get_password(_account(provider, settings), provider)
        if key:
            return key
    except Exception:  # pragma: no cover - keyring backend unavailable
        pass
    if provider == PROVIDER_OPENAI:
        return os.getenv("OPENAI_API_KEY") or None
    return None


def credential_status(session: Session, provider: str, settings: Settings | None = None) -> dict[str, Any]:
    """Configured/not-configured metadata. Never returns the secret."""
    settings = settings or get_settings()
    metadata = session.scalar(
        select(CredentialMetadata).where(CredentialMetadata.provider == provider)
    )
    present = resolve_api_key(provider, settings) is not None
    env_fallback = provider == PROVIDER_OPENAI and bool(os.getenv("OPENAI_API_KEY"))
    return {
        "provider": provider,
        "configured": bool(metadata and metadata.configured) or present,
        "storage_method": metadata.storage_method if metadata else ("environment" if env_fallback else "keychain"),
        "last_updated_at": metadata.last_updated_at if metadata else None,
        "last_validated_at": metadata.last_validated_at if metadata else None,
        "validation_result": metadata.validation_result if metadata else None,
        "via_environment": env_fallback and not (metadata and metadata.configured),
    }


def store_credential(
    session: Session,
    provider: str,
    secret_value: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store a credential in the OS credential store and record metadata only."""
    if not secret_value or not secret_value.strip():
        raise ValueError("credential value may not be empty")
    settings = settings or get_settings()
    keyring.set_password(_account(provider, settings), provider, secret_value.strip())
    metadata = session.scalar(
        select(CredentialMetadata).where(CredentialMetadata.provider == provider)
    )
    now = datetime.now(UTC)
    if metadata is None:
        metadata = CredentialMetadata(provider=provider)
    metadata.storage_method = "keychain"
    metadata.configured = True
    metadata.last_updated_at = now
    session.add(metadata)
    session.commit()
    return credential_status(session, provider, settings)


def remove_credential(session: Session, provider: str, settings: Settings | None = None) -> None:
    """Remove the credential from the OS credential store and mark unconfigured."""
    settings = settings or get_settings()
    try:
        keyring.delete_password(_account(provider, settings), provider)
    except keyring.errors.PasswordDeleteError:
        pass
    metadata = session.scalar(
        select(CredentialMetadata).where(CredentialMetadata.provider == provider)
    )
    if metadata is not None:
        metadata.configured = False
        metadata.last_updated_at = datetime.now(UTC)
        session.add(metadata)
        session.commit()


def validate_credential(
    session: Session,
    provider: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Validate the credential without displaying it.

    Records a sanitized validation result and timestamp only. This performs no
    provider call in Phase 1.3 — presence validation only.
    """
    settings = settings or get_settings()
    present = resolve_api_key(provider, settings) is not None
    metadata = session.scalar(
        select(CredentialMetadata).where(CredentialMetadata.provider == provider)
    )
    if metadata is None:
        metadata = CredentialMetadata(provider=provider, configured=present)
    metadata.last_validated_at = datetime.now(UTC)
    metadata.validation_result = sanitize_message(
        "credential present in secure store" if present else "credential not found"
    )
    session.add(metadata)
    session.commit()
    return {"present": present, "validated_at": metadata.last_validated_at}
