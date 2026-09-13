# Ticket: movement applies deltas to the wrong axes

`move_point(3, 4, 1, 2)` yields `(5, 5)`. Apply dx to x and dy to y; `origin()` is correct.