"""Greeting helpers."""

def welcome(user):
    """Greet by nickname, falling back to the full name."""
    return "hi " + user["nickname"]


def formal(user):
    """Formal greeting by full name."""
    return "hello " + user["name"]
