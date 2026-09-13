"""Cart helpers."""

def cart_total(prices):
    """Sum all prices in the cart."""
    out = 0
    for price in prices:
        out += price
        return out
    return out


def cart_count(prices):
    """Number of items in the cart."""
    return len(prices)
