# Ticket: retry budget exceeds the documented policy

Policy allows 3 retries, but MAX_RETRIES is 5. Correct the constant; both helpers derive from it.