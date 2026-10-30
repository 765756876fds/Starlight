import os
from types import SimpleNamespace

from app.movie.history import MovieHistory
from app.movie.movie_controller import MovieController


def test_movie_history_deduplicates_and_caps(tmp_path):
    history = MovieHistory(str(tmp_path / "movie.db"))
    for i in range(55):
        history.record(media_key=f"k{i}", title=f"T{i}", media_type="local",
                       replay_value=str(tmp_path / f"{i}.mp4"), played_at=float(i))
    rows = history.list()
    assert len(rows) == 50
    assert rows[0]["title"] == "T54"
    assert rows[-1]["title"] == "T5"
    history.record(media_key="k54", title="T54-new", media_type="local",
                   replay_value="new.mp4", played_at=100.0)
    assert history.list()[0]["title"] == "T54-new"
    assert len(history.list()) == 50


def test_movie_history_delete_and_persist(tmp_path):
    path = tmp_path / "movie.db"
    first = MovieHistory(str(path))
    row_id = first.record(media_key="x", title="X", media_type="online",
                          replay_value="https://example.invalid/x")
    assert first.delete(row_id)
    assert first.list() == []
    first.record(media_key="y", title="Y", media_type="online", replay_value="u")
    assert MovieHistory(str(path)).list()[0]["title"] == "Y"


def test_local_subtitle_selection_is_exact_stem_and_priority(tmp_path):
    video = tmp_path / "中文 片名.MKV"
    video.write_bytes(b"video")
    ass = tmp_path / "中文 片名.ASS"
    ass.write_bytes(b"ass")
    srt = tmp_path / "中文 片名.srt"
    srt.write_bytes(b"srt")
    assert MovieController._local_subtitle(str(video)) == str(srt)
    srt.unlink()
    assert os.path.normcase(MovieController._local_subtitle(str(video))) == os.path.normcase(str(ass))
    (tmp_path / "中文 片名.zh.srt").write_bytes(b"other")
    assert os.path.normcase(MovieController._local_subtitle(str(video))) == os.path.normcase(str(ass))


def test_local_subtitle_similarity_rules_and_ambiguity(tmp_path):
    video = tmp_path / "Amazing.Movie.2024.1080p.mkv"
    video.write_bytes(b"video")
    letters = tmp_path / "Amazing.Movie.2024.1080p.English.srt"
    letters.write_bytes(b"srt")
    assert MovieController._local_subtitle(str(video)) == str(letters)

    letters.unlink()
    digits = tmp_path / "Other.Title.2024.1080p.ass"
    digits.write_bytes(b"ass")
    assert MovieController._local_subtitle(str(video)) == str(digits)

    digits.unlink()
    text = tmp_path / "精彩电影字幕版.vtt"
    text.write_bytes(b"vtt")
    video.unlink()
    video = tmp_path / "精彩电影.mkv"
    video.write_bytes(b"video")
    assert MovieController._local_subtitle(str(video)) == str(text)

    text.unlink()
    (tmp_path / "精彩电影 简体.srt").write_bytes(b"1")
    (tmp_path / "精彩电影 繁体.srt").write_bytes(b"2")
    assert MovieController._local_subtitle(str(video)) is None


def test_local_subtitle_no_match_is_silent(tmp_path):
    video = tmp_path / "A.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Completely.Unrelated.Movie.srt").write_bytes(b"srt")
    assert MovieController._local_subtitle(str(video)) is None


def test_subtitle_character_categories_are_separate():
    assert MovieController._subtitle_similarity("abcdef-film", "abcdef-film-en")[0] == 3
    # 五个相同汉字不能进入英文规则，但可以进入中文文字规则。
    assert MovieController._subtitle_similarity("电影电影电影电影电影", "电影电影电影电影电影字幕")[0] == 1
    assert MovieController._subtitle_similarity("电影电影电影", "电影电影电影字幕")[0] == 1
    # 混合名称仍按英文规则优先。
    assert MovieController._subtitle_similarity("abcdef电影123", "abcdef电影123字幕")[0] == 3


def test_subtitle_similarity_gap_is_conservative(tmp_path):
    video = tmp_path / "Amazing.Movie.2024.mkv"
    video.write_bytes(b"video")
    close_a = tmp_path / "Amazing.Movie.2024.English.srt"
    close_b = tmp_path / "Amazing.Movie.2024.Chinese.srt"
    close_a.write_bytes(b"a")
    close_b.write_bytes(b"b")
    assert MovieController._local_subtitle(str(video)) is None

    close_b.unlink()
    clear = tmp_path / "Amazing.Movie.2024.English.Full.Release.srt"
    clear.write_bytes(b"c")
    assert MovieController._local_subtitle(str(video)) == str(close_a)


def test_subtitle_exact_name_and_format_priority(tmp_path):
    video = tmp_path / "电影 01.MKV"
    video.write_bytes(b"video")
    ass = tmp_path / "电影 01.ASS"
    srt = tmp_path / "电影 01.srt"
    ass.write_bytes(b"ass")
    srt.write_bytes(b"srt")
    assert MovieController._local_subtitle(str(video)) == str(srt)


def test_history_row_playback_uses_double_click_shared_entrypoint():
    calls = []
    panel = SimpleNamespace(
        _movie=SimpleNamespace(history_item=lambda row: row),
        _play_item=lambda item: calls.append(item),
        lbl_status=SimpleNamespace(setText=lambda text: calls.append(text)),
    )
    row = {"id": 7, "title": "当前记录", "media_type": "local"}
    # 调用真实面板方法，验证按钮/双击共用的播放逻辑只使用传入记录。
    from app.ui.panels.movie_panel import MoviePanel
    assert MoviePanel._play_movie_history_row(panel, row) is True
    assert calls[0] is row


def test_history_item_reconstructs_local_and_online_entries(tmp_path):
    video = tmp_path / "历史 视频.mp4"
    video.write_bytes(b"video")
    owner = SimpleNamespace()
    local = MovieController.history_item(owner, {
        "title": "本地", "media_type": "local", "replay_value": str(video),
        "media_key": "local-key",
    })
    online = MovieController.history_item(owner, {
        "title": "在线", "media_type": "online", "replay_value": "https://x/video",
        "media_key": "online-key",
    })
    missing = MovieController.history_item(owner, {
        "title": "失效", "media_type": "local",
        "replay_value": str(tmp_path / "missing.mp4"),
    })
    assert local["local_file"] is True and local["first_url"] == str(video)
    assert online["local_file"] is False and online["first_url"] == "https://x/video"
    assert missing is None


def test_history_is_recorded_only_after_idle_becomes_false():
    recorded = []
    controller = SimpleNamespace(
        _pending_history=({"vod_name": "Film", "local_file": False,
                           "vod_id": "film-1"}, "https://media/film"),
        _history_recorded_gen=None, _playback_gen=1,
        history=SimpleNamespace(record=lambda **kw: recorded.append(kw)),
    )
    MovieController._record_history_if_started(controller, True)
    assert recorded == []
    MovieController._record_history_if_started(controller, False)
    MovieController._record_history_if_started(controller, False)
    assert len(recorded) == 1
    assert recorded[0]["media_type"] == "online"

