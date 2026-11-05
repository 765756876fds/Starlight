from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.movie.discovery import BilibiliRank, MovieDiscovery, merge_works, official_url
from app.movie.library import MovieLibrary, resume_position


def work(**changes):
    row = {"title": "示例电影", "year": 2024, "kind": "movie", "region": "中国", "season": None,
           "platform": "bilibili", "platform_name": "B站", "rank": 3, "source_id": "1",
           "url": "https://www.bilibili.com/bangumi/play/ss1", "board": "电影"}
    return dict(row, **changes)


def test_same_complete_film_merges_platforms_and_preserves_each_rank_without_mutating_input():
    rows = [work(), work(platform="iqiyi", platform_name="爱奇艺", rank=5,
                         url="https://www.iqiyi.com/v_1.html")]
    old = deepcopy(rows)
    merged = merge_works(rows)
    assert len(merged) == 1 and [r["rank"] for r in merged[0]["sources"]] == [3, 5]
    assert rows == old


@pytest.mark.parametrize("changes", [{"year": 2023}, {"year": None}, {"region": "美国"},
                                      {"region": None}, {"kind": "tv"}, {"title": "示例电影2"}])
def test_uncertain_or_different_works_are_not_merged(changes):
    assert len(merge_works([work(), work(**changes)])) == 2


@pytest.mark.parametrize("first,second,count", [(1, 2, 2), (1, 1, 1), (None, None, 2)])
def test_seasons_and_unknown_seasons_are_conservative(first, second, count):
    assert len(merge_works([work(kind="tv", season=first), work(kind="tv", season=second)])) == count


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://www.bilibili.com.evil.example/a", "http://www.bilibili.com/a", "file:///private/file"])
def test_official_links_cannot_be_replaced_with_other_origins(url):
    assert not official_url(url, "bilibili")


def test_public_bilibili_normalization_does_not_invent_release_year_or_playback_url():
    provider = BilibiliRank(fetch=lambda _: {"code": 0, "data": {"list": [
        {"title": "示例作品", "season_id": 123, "rank": 2, "url": "https://www.bilibili.com/bangumi/play/ss123"}]}})
    row = provider.read("movie")[0]
    assert row["rank"] == 2 and row["kind"] == "movie" and row["year"] is None
    assert "play_url" not in row and row["url"].startswith("https://www.bilibili.com/")


@pytest.mark.parametrize("payload", [{"code": -412}, {"code": 0, "data": {}}, None])
def test_business_errors_and_invalid_schema_cannot_be_normal_empty_rank(payload):
    with pytest.raises(ValueError):
        BilibiliRank(fetch=lambda _: payload).read("movie")


def test_partial_failure_stale_cache_and_immediate_failed_retry(tmp_path):
    clock = [1000]
    calls = {"good": 0, "bad": 0}
    fail_good = [False]
    def good(_):
        calls["good"] += 1
        if fail_good[0]:
            raise requests.Timeout()
        return [work()]
    def bad(_):
        calls["bad"] += 1
        raise requests.ConnectionError()
    providers = [SimpleNamespace(key="good", name="正常源", categories=["movie"], read=good),
                 SimpleNamespace(key="bad", name="失败源", categories=["movie"], read=bad)]
    service = MovieDiscovery(tmp_path / "cache.json", providers, clock=lambda: clock[0])
    first = service.read("movie")
    assert len(first["works"]) == 1 and {s["state"] for s in first["states"]} == {"success", "failed"}
    service.read("movie")
    assert calls == {"good": 1, "bad": 2}, "失败源可以立即重试，正常缓存仍命中"
    clock[0] += 1900
    fail_good[0] = True
    result = service.read("movie")
    assert len(result["works"]) == 1
    assert next(s for s in result["states"] if s["platform"] == "正常源")["state"] == "stale"
    assert all(s.get("error") for s in result["states"])


def test_empty_success_can_be_cached_and_restart_retains_timestamp(tmp_path):
    read = lambda _: []
    provider = SimpleNamespace(key="empty", name="空榜单", categories=["movie"], read=read)
    path = tmp_path / "cache.json"
    service = MovieDiscovery(path, [provider], clock=lambda: 10)
    assert service.read("movie")["states"][0]["state"] == "empty"
    restarted = MovieDiscovery(path, [provider], clock=lambda: 20)
    assert restarted.read("movie")["states"][0] == {"platform": "空榜单", "kind": "movie", "state": "cached", "at": 10}


def test_local_index_scans_selected_folders_and_keeps_added_time_and_missing_records(tmp_path):
    folder = tmp_path / "films"
    folder.mkdir()
    nested = folder / "season"
    nested.mkdir()
    (folder / "示例电影.2024.mkv").write_bytes(b"not-decoded")
    (nested / "示例剧.S02E03.mp4").write_bytes(b"not-decoded")
    (nested / "ignore.mp3").write_bytes(b"not-video")
    library = MovieLibrary(tmp_path / "index.json", clock=lambda: 10)
    first = library.scan([str(folder), str(nested)])
    assert first["count"] == 2
    assert {row["kind"] for row in first["entries"]} == {"unknown", "tv"}
    library.clock = lambda: 100
    assert all(row["added_at"] == 10 for row in library.scan([str(folder)])["entries"])
    matches = library.find({"title": "示例电影", "year": 2024})
    assert len(matches) == 1 and not library.find({"title": "示例电影", "year": 2023})
    result = library.scan([str(tmp_path / "temporarily-offline")])
    assert result["issues"] and len(result["entries"]) == 2 and not any(r["available"] for r in result["entries"])
    assert (folder / "示例电影.2024.mkv").read_bytes() == b"not-decoded"


def test_empty_directory_is_not_reported_as_an_indexing_failure(tmp_path):
    library = MovieLibrary(tmp_path / "index.json")
    result = library.scan([str(tmp_path)])
    assert result["count"] == 0 and result["issues"] == []


def test_resume_uses_existing_native_checkpoint_and_never_guesses_by_basename(tmp_path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"media-placeholder")
    watch = tmp_path / "watch"
    watch.mkdir()
    native_name = hashlib.md5(str(video).encode(), usedforsecurity=False).hexdigest().upper()
    file = watch / native_name
    file.write_text("start=42.5\n", encoding="utf-8")
    history = [{"title": "电影", "media_type": "local", "replay_value": str(video)}]
    assert MovieLibrary.continuing(history, watch)[0]["resume_seconds"] == 42.5
    file.unlink()  # This test's own generated checkpoint, modeling MPV's EOF cleanup.
    assert MovieLibrary.continuing(history, watch) == []
    base = hashlib.md5(video.name.encode(), usedforsecurity=False).hexdigest().upper()
    (watch / base).write_text("start=88\n", encoding="utf-8")
    assert resume_position(video, watch) is None


def test_movie_folder_preference_preserves_theme_and_other_existing_fields(tmp_path, monkeypatch):
    from app.ui import settings_store
    path = tmp_path / "prefs.json"
    path.write_text(json.dumps({"theme": "light", "equip_notify": False, "future_field": {"keep": True}}), encoding="utf8")
    monkeypatch.setattr(settings_store, "UI_PREFS_PATH", str(path))
    assert settings_store.save_ui_prefs({"movie_library_dirs": [str(tmp_path / "films")]})
    saved = json.loads(path.read_text(encoding="utf8"))
    assert saved["theme"] == "light" and saved["equip_notify"] is False
    assert saved["future_field"] == {"keep": True}
    assert settings_store.load_ui_prefs()["movie_library_dirs"] == [str(tmp_path / "films")]
    saved["movie_library_dirs"] = "not-a-list"
    path.write_text(json.dumps(saved), encoding="utf8")
    assert settings_store.load_ui_prefs()["movie_library_dirs"] == []
