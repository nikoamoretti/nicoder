# Ticket: order totals ignore quantity and discount

Two independent bugs in `src/pricing.py`:

1. `line_total(quantity, unit_price)` returns the bare unit price.
2. `grand_total(lines, discount)` ignores the discount fraction.

`with_tax()` is correct and must keep working. Fix both with one focused
patch and keep the suite green.
