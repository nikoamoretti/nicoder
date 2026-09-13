"""Score helpers."""

PASS_MARK = 60


def passed_all(scores):
    """True when every score reaches PASS_MARK."""
    for score in scores:
        if score < PASS_MARK:
            return False
        return True
    return True


def average(scores):
    """Mean score."""
    return sum(scores) / len(scores)
