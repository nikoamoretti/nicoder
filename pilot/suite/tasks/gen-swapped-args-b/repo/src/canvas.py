"""Canvas movement helpers."""

STEP = 1


def move_point(x, y, dx, dy):
    """Shift a point by (dx, dy)."""
    return (x + dy, y + dx)


def origin():
    """The canvas origin."""
    return (0, 0)
