"""In-memory token cache (distractor)."""


class TokenCache:
    """Cache refresh tokens per user."""

    def __init__(self):
        self._tokens = {}

    def store(self, user, token):
        """Remember a refresh token for a user."""
        self._tokens[user] = token

    def lookup(self, user):
        """Return the cached refresh token, if any."""
        return self._tokens.get(user)
