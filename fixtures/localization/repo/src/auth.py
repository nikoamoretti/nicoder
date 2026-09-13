"""Authentication helpers (distractor: token/session vocabulary, no bug)."""

import hashlib


def authenticate(username, password):
    """Return a session token for valid credentials."""
    digest = hashlib.sha256(f"{username}:{password}".encode()).hexdigest()
    return {"user": username, "token": digest[:16]}


def verify_token(session, token):
    """Check that a session carries the expected token."""
    return session.get("token") == token
