# Ticket: discount computes the taken-off part, not the price

`apply_discount(80, 0.5)` returns 40 only by coincidence of the test values... actually it returns the discount amount instead of the final price. Charge `price * (1 - fraction)`; `loyalty_price()` is correct.