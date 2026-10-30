"""Small, strict number grammar for command parameters (no guessed defaults)."""
import re

_DIGITS = {char: index for index, char in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "两": 2})
_NONZERO = "一二两三四五六七八九"
_INTEGER = re.compile(
    rf"(?:[{_NONZERO}]百(?:零[{_NONZERO}]|[{_NONZERO}]十(?:[{_NONZERO}])?)?"
    rf"|[{_NONZERO}]?十[{_NONZERO}]?|[零〇{_NONZERO}])")


def parse_integer(text):
    if not isinstance(text, str) or len(text) > 32:
        return None
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    if not _INTEGER.fullmatch(text):
        return None
    total, digit = 0, 0
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS[char]
        else:
            total += (digit or 1) * (100 if char == "百" else 10)
            digit = 0
    return total + digit


def parse_number(text):
    if not isinstance(text, str) or len(text) > 32:
        return None
    if text == "半":
        return 0.5
    if re.fullmatch(r"[0-9]+\.[0-9]+", text):
        return float(text)
    if "点" in text:
        whole, fraction = text.split("点", 1)
        integer = parse_integer(whole)
        if integer is None or not fraction or any(c not in _DIGITS for c in fraction):
            return None
        return float(f"{integer}." + "".join(str(_DIGITS[c]) for c in fraction))
    integer = parse_integer(text)
    return float(integer) if integer is not None else None
