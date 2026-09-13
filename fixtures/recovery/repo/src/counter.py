"""A tiny counter used to exercise patch recovery."""

INCREMENT = 2


class Counter:
    """Count up from zero with an explicit reset."""

    def __init__(self):
        self.value = 0

    def increment(self):
        """Advance the counter by one step."""
        self.value += INCREMENT
        return self.value

    def reset(self):
        """Return the counter to zero."""
        self.value = 0
        return self.value
