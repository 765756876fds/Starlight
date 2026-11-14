"""Only interpret metadata that a public provider actually supplies."""
import re


def bilibili_metadata(item):
    value = item.get("rating")
    if isinstance(value, dict):
        value = value.get("score")
    score = None
    if type(value) in (str, int, float) and re.fullmatch(r"(?:10|[0-9])(?:\.[0-9]+)?(?:分)?", str(value)):
        number = float(str(value).removesuffix("分"))
        if 0 < number <= 10:
            score = number
    return dict(score=score, score_source="哔哩哔哩" if score is not None else "", genres=[])
