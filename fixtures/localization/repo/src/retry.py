"""Retry helpers for flaky session refresh calls (distractor)."""


def with_retry(call, attempts=3):
    """Run call until it succeeds or attempts run out."""
    last_error = None
    for _ in range(attempts):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - demo fixture
            last_error = exc
    raise last_error


def backoff_seconds(attempt):
    """Exponential backoff in seconds."""
    return 2 ** attempt
