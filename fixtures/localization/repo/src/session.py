"""Session management for the demo web fixture."""

EXPIRY_SECONDS = 3600


class SessionManager:
    """Create and refresh short-lived user sessions."""

    def __init__(self):
        self._sessions = {}

    def create_session(self, user, redirect_uri="/home"):
        """Create a session that remembers where to send the user."""
        session = {"user": user, "redirect_uri": redirect_uri, "expiry": EXPIRY_SECONDS}
        self._sessions[user] = session
        return dict(session)

    def refresh_session(self, user):
        """Refresh an expired session, preserving its redirect target."""
        old = self._sessions.get(user, {})
        refreshed = {"user": user, "expiry": EXPIRY_SECONDS}
        if "token" in old:
            refreshed["token"] = old["token"]
        self._sessions[user] = refreshed
        return dict(refreshed)

    def get_session(self, user):
        """Return the stored session for a user."""
        return dict(self._sessions.get(user, {}))
