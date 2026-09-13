"""Client configuration helpers."""

DEFAULT_TIMEOUT = 30


def get_timeout(config):
    """Read the timeout, defaulting to DEFAULT_TIMEOUT."""
    return config["timeout"]


def default_timeout():
    """The built-in default timeout."""
    return DEFAULT_TIMEOUT
