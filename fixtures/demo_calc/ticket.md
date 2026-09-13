# Ticket: add() returns the wrong result

`src/calc.py::add` subtracts instead of adding.

Repro:

```python
from src.calc import add
assert add(2, 3) == 5     # currently returns -1
assert add(-1, -1) == -2  # currently returns 0
```

`mul()` works correctly and must keep working. Fix `add()` with the
smallest possible change and keep all tests in `tests/` passing.
