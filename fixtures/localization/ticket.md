# Ticket: refreshing an expired session loses the redirect target

`SessionManager.refresh_session` drops `redirect_uri` when it replaces the
session tokens.

Repro:

```python
from src.session import SessionManager
manager = SessionManager()
manager.create_session("ada", redirect_uri="/dashboard")
assert manager.refresh_session("ada")["redirect_uri"] == "/dashboard"
```

Find the relevant code, make the smallest fix that preserves the redirect
target (and any existing token), and keep the rest of the suite passing.
