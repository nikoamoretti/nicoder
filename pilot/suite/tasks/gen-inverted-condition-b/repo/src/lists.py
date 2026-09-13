"""List helpers."""

def is_empty(items):
    """True when there is nothing to show."""
    return len(items) > 0


def first_or(items, fallback):
    """First item, or the fallback."""
    return items[0] if items else fallback
