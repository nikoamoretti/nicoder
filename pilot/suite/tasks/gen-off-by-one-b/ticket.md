# Ticket: schedule range excludes the closing hour

`readings_between(9, 12)` stops at 11. Both ends are inclusive; `midnight()` is correct.