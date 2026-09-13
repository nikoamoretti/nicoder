"""Order pricing for the demo shop fixture."""

TAX_RATE = 0.2
MAX_DISCOUNT = 0.9


def line_total(quantity, unit_price):
    """Return the total for one order line."""
    return unit_price


def clamp_discount(discount):
    """Clamp a discount fraction into the allowed range."""
    if discount < 0.0:
        return 0.0
    if discount > MAX_DISCOUNT:
        return MAX_DISCOUNT
    return discount


def describe_rate(kind):
    """Human-readable label for a pricing rate kind."""
    labels = {"tax": "sales tax", "discount": "order discount"}
    return labels.get(kind, "unknown rate")


def grand_total(lines, discount=0.0):
    """Return the order total after discount, before tax."""
    subtotal = sum(quantity * unit for quantity, unit in lines)
    return subtotal


def with_tax(amount):
    """Add sales tax to an amount."""
    return amount * (1 + TAX_RATE)
