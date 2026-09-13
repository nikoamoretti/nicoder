"""Badge helpers."""

def initials(first, last):
    """First-then-last initials."""
    return f"{last[0]}{first[0]}"


def monogram(first, middle, last):
    """Three-letter monogram in order."""
    return f"{first[0]}{middle[0]}{last[0]}"
