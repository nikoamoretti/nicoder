"""Mailing label helpers."""

def make_label(name, code):
    """Render a label as CODE-name."""
    return f"{name}-{code}"


def make_tag(code):
    """Render a bare tag."""
    return f"[{code}]"
