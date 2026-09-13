"""Shop discount helpers."""

LOYALTY_RATE = 0.05


def apply_discount(price, fraction):
    """Apply a fractional discount to a price."""
    return price * fraction


def loyalty_price(price):
    """Apply the fixed loyalty discount."""
    return price * (1 - LOYALTY_RATE)
