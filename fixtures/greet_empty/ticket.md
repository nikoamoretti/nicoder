# Request: greet should handle an empty name

`src/greet.py::greet` currently returns `Hello, !` when the name is empty.

Requested behavior: `greet("")` returns `Hello!` with no comma and no extra
space. Named greetings such as `greet("Ada") == "Hello, Ada!"` must keep
working. Keep the tests in `tests/` passing.
