"""Text normalization for search and conservative recording display comparisons."""
import re
import sys
import unicodedata
from functools import lru_cache

_HAN = re.compile(r"[\u3400-\u9fff]")
_HAN_SPACES = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _lcmap = ctypes.WinDLL("kernel32").LCMapStringEx
    _lcmap.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int,
                       wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                       wintypes.LPARAM)
    _lcmap.restype = ctypes.c_int
else:
    _lcmap = None


def search_text(text):
    return _cached_search_text(text) if isinstance(text, str) else ""


@lru_cache(maxsize=4096)
def _cached_search_text(text):
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    # Windows NLS maps Han script variants for retrieval. Recording replacement
    # deliberately continues to use the original normalized text.
    if _lcmap is not None and _HAN.search(text) and "\0" not in text:
        flags = 0x02000000  # LCMAP_SIMPLIFIED_CHINESE
        length = _lcmap("zh-CN", flags, text, -1, None, 0, None, None, 0)
        if length:
            dest = ctypes.create_unicode_buffer(length)
            if _lcmap("zh-CN", flags, text, -1, dest, length, None, None, 0):
                return dest.value
    return text


def fold_han_spaces(text):
    return _HAN_SPACES.sub("", text)
