"""API paging helpers."""

DEFAULT_LIMIT = 50


def page_params(page):
    """Paging parameters honoring the documented default limit."""
    return {"page": page, "limit": DEFAULT_LIMIT}


def max_pages(total):
    """Page count for a result total."""
    return (total + DEFAULT_LIMIT - 1) // DEFAULT_LIMIT
