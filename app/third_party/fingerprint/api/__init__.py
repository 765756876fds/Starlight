"""Flask REST API and bundled web UI.

The desktop recognizer only needs ``fingerprint.api.runtime``.  Keep Flask
optional by loading the web application lazily instead of importing it while
the package is initialized.
"""

from .runtime import Runtime


def create_app(*args, **kwargs):
    """Create the optional Flask application on explicit web-server use."""
    from .app import create_app as _create_app
    return _create_app(*args, **kwargs)

__all__ = ["Runtime", "create_app"]
