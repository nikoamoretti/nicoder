"""Acceptance tests for session refresh behavior."""

from src.session import SessionManager


def test_refresh_preserves_redirect():
    manager = SessionManager()
    manager.create_session("ada", redirect_uri="/dashboard")
    refreshed = manager.refresh_session("ada")
    assert refreshed["redirect_uri"] == "/dashboard"


def test_create_session():
    manager = SessionManager()
    session = manager.create_session("ada")
    assert session["user"] == "ada"
