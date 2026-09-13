"""Upload retry helpers."""

MAX_RETRIES = 5


def retries_left(attempt):
    """Retries remaining after an attempt number."""
    return MAX_RETRIES - attempt


def may_retry(attempt):
    """Whether another attempt is allowed."""
    return attempt < MAX_RETRIES
