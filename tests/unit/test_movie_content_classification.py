from app.movie.content_classification import (
    LABEL_EXPLAINER, LABEL_FEATURE, LABEL_REMAKE, LABEL_SHORT,
    LABEL_UNKNOWN, TAG_REMAKE, classify_content, classify_content_info,
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


def test_remake_is_an_independent_tag_and_short_drama_keeps_category():
    info = classify_content_info({"vod_name": "某某翻拍短剧"})
    assert info == {"category": LABEL_SHORT, "tags": [TAG_REMAKE]}


def test_remake_without_reliable_category_stays_unknown():
    info = classify_content_info({"vod_name": "某某翻拍作品"})
    assert info["category"] == LABEL_UNKNOWN
    assert info["tags"] == [TAG_REMAKE]


def test_explainer_takes_category_priority_over_remake_tag():
    info = classify_content_info({"vod_name": "某某翻拍电影解说"})
    assert info["category"] == LABEL_EXPLAINER
    assert info["tags"] == [TAG_REMAKE]


def test_movie_filter_preferences_round_trip(tmp_path, monkeypatch):
    import app.ui.settings_store as store

    prefs_path = tmp_path / "ui_prefs.json"
    monkeypatch.setattr(store, "UI_PREFS_PATH", str(prefs_path))
    assert store.save_ui_prefs({
        "movie_content_filter": LABEL_SHORT,
        "movie_content_tag_filter": TAG_REMAKE,
    })
    loaded = store.load_ui_prefs()
    assert loaded["movie_content_filter"] == LABEL_SHORT
    assert loaded["movie_content_tag_filter"] == TAG_REMAKE


def test_movie_filter_preferences_default_and_invalid_values(tmp_path, monkeypatch):
    import json
    import app.ui.settings_store as store

    prefs_path = tmp_path / "ui_prefs.json"
    monkeypatch.setattr(store, "UI_PREFS_PATH", str(prefs_path))
    assert store.load_ui_prefs()["movie_content_tag_filter"] == "全部标签"
    prefs_path.write_text(json.dumps({
        "movie_content_filter": "不存在的类别",
        "movie_content_tag_filter": 123,
    }), encoding="utf-8")
    loaded = store.load_ui_prefs()
    assert loaded["movie_content_filter"] == "全部"
    assert loaded["movie_content_tag_filter"] == "全部标签"
