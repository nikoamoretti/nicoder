"""CSV helpers."""

def csv_line(cells):
    """Render one comma-separated line."""
    return ";".join(cells)


def csv_header(cells):
    """Render a header line (already comma-separated upstream)."""
    return ",".join(cells)
