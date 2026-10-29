from app.movie.content_classification import (
    LABEL_EXPLAINER, LABEL_FEATURE, LABEL_REMAKE, LABEL_SHORT,
    LABEL_UNKNOWN, classify_content,
)


def test_explicit_source_type_wins():
    assert classify_content({"vod_type": "电影", "vod_name": "任意"}) == LABEL_FEATURE
    assert classify_content({"vod_class": "电影解说"}) == LABEL_EXPLAINER


def test_conservative_title_fallback():
    assert classify_content({"vod_name": "某某翻拍短剧"}) == LABEL_REMAKE
    assert classify_content({"vod_remarks": "精彩电影解说"}) == LABEL_EXPLAINER
    assert classify_content({"vod_name": "微短剧合集"}) == LABEL_SHORT
    assert classify_content({"vod_name": "普通条目"}) == LABEL_UNKNOWN


def test_malformed_result_degrades_to_unknown():
    assert classify_content(None) == LABEL_UNKNOWN
    assert classify_content({"vod_name": None, "vod_remarks": 123}) == LABEL_UNKNOWN
