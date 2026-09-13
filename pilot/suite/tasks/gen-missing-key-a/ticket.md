# Ticket: missing timeout raises instead of defaulting

`get_timeout({})` raises KeyError. Fall back to DEFAULT_TIMEOUT; `default_timeout()` is correct.