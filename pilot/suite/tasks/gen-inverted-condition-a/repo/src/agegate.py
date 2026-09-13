"""Age gate helpers."""

LIMIT = 18


def is_adult(age):
    """True once LIMIT is reached."""
    return age < LIMIT


def limit():
    """The age limit."""
    return LIMIT
