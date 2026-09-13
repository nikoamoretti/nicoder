"""Thermostat schedule helpers."""

def readings_between(low, high):
    """Hourly readings from low to high, inclusive of both ends."""
    return list(range(low, high))


def midnight():
    """First hour of the day."""
    return 0
