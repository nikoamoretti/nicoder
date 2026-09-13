# Ticket: greeting crashes without a nickname

`welcome({"name": "Ada L"})` raises KeyError. Fall back to the full name; `formal()` is correct.