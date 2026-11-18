"""Per-process anonymous source identifiers, extracted unchanged for search."""
import hashlib
import os

_SOURCE_SALT = os.urandom(16)


def source_tag(value):
    """Per-process anonymous source correlation; never a URL/title in the log."""
    if isinstance(value, str) and "://" in value:
        from urllib.parse import urlsplit
        try:
            value = urlsplit(value).hostname
        except ValueError:
            value = None
    return (hashlib.blake2s(value.encode("utf-8"), key=_SOURCE_SALT, digest_size=6).hexdigest()
            if isinstance(value, str) and value else "unknown")
