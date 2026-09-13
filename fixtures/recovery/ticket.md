# Ticket: counter advances by two instead of one

`Counter.increment()` adds `INCREMENT` (currently 2) instead of a single
step. `reset()` is correct and must keep working — a fix that breaks reset
is a regression, not a solution.
