"""P1 候选旁路 + 换源续播测试。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest


def _make_controller(tmp_path):
    from app.music.controller import MusicController
    cfg = {
        "database": str(tmp_path / "t.db"),
        "scan_dirs": [],
        "extensions": [".mp3"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_p1",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"), \
         patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        c = MusicController(cfg)
    c.mpv = MagicMock()
    c.mpv.get_live_playback_time.return_value = 42.0
    return c


def _track(name="T", artist="A", source="kw"):
    from app.music.online.models import OnlineTrack
    return OnlineTrack(source_name=source, track_id="1", title=name, artist=artist)


def test_publish_candidates_event_payload(tmp_path):
    c = _make_controller(tmp_path)
    tracks = [_track("A"), _track("B", source="wy")]
    with patch.object(c.event_bus, "publish") as pub:
        c._publish_candidates("q", tracks)
    assert pub.called
    event = pub.call_args[0][0]
    d = event.data
    assert d["query"] == "q"
    assert d["generation"] == 1
    assert d["display"] == [
        {"idx": 0, "source": "kw", "title": "A", "artist": "A"},
        {"idx": 1, "source": "wy", "title": "B", "artist": "A"},
    ]
    for item in d["display"]:
        assert isinstance(item, dict)
    assert len(c._active_candidates) == 2


def test_publish_candidates_generation_increments(tmp_path):
    c = _make_controller(tmp_path)
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q1", [_track()])
        c._publish_candidates("q2", [_track()])
    assert c._candidate_generation == 2


def test_play_online_resume_generation_mismatch(tmp_path):
    c = _make_controller(tmp_path)
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q", [_track()])
    ok = c.play_online_resume(0, 0)
    assert ok is False
    c.mpv.loadfile.assert_not_called()


def test_play_online_resume_idx_out_of_range(tmp_path):
    c = _make_controller(tmp_path)
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q", [_track()])
    ok = c.play_online_resume(c._candidate_generation, 5)
    assert ok is False


def test_play_online_resume_success(tmp_path):
    """B 异步换源：立即返回 True，后台线程完成 loadfile，从 0:00 重播（无 seek）。"""
    import time as _real
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    c.mpv.get_property.return_value = False  # idle-active=False，播起来了
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q", [_track()])
    gen = c._candidate_generation

    with patch("time.sleep", return_value=None):
        ok = c.play_online_resume(gen, 0)
        assert ok is True
    for _ in range(80):
        if not c._candidate_switching:
            break
        _real.sleep(0.05)

    c.mpv.loadfile.assert_called()
    c.mpv.seek.assert_not_called()

def test_play_online_resume_cancel_token(tmp_path):
    """用户操作令牌：_cancel_candidate_switch 使 token+1 且清标记。"""
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = None  # 全失败
    c.mpv.get_property.return_value = True  # 没播起来
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q", [_track(), _track(source="wy")])
    gen = c._candidate_generation

    with patch("time.sleep", return_value=None):
        c.play_online_resume(gen, 0)
        tok0 = c._candidate_switch_token
        c._cancel_candidate_switch()
        assert c._candidate_switch_token == tok0 + 1
        assert c._candidate_switching is False

def test_play_online_resume_all_fail_publishes_error(tmp_path):
    """全部源失败 -> 发 ONLINE_ERROR 事件（不跳下一首）。"""
    import time as _real
    from app.music.online.events import ONLINE_ERROR
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = None
    c.mpv.get_property.return_value = True

    published = []
    def _cap(ev):
        published.append(ev)
    with patch.object(c.event_bus, "publish", side_effect=_cap):
        c._publish_candidates("q", [_track()])
        gen = c._candidate_generation
        with patch("time.sleep", return_value=None):
            c.play_online_resume(gen, 0)
        for _ in range(80):
            if not c._candidate_switching:
                break
            _real.sleep(0.05)

    err_evts = [e for e in published if getattr(e, "event_type", None) == ONLINE_ERROR]
    assert len(err_evts) == 1
    assert "无法播放" in err_evts[0].data.get("message", "")

def test_cancel_on_user_action(tmp_path):
    """用户操作入口（如 pause）触发取消换源（装饰器生效）。"""
    c = _make_controller(tmp_path)
    c.mpv = MagicMock()
    c._candidate_switching = True
    c._candidate_switch_token = 5
    c.pause()
    assert c._candidate_switch_token == 6
    assert c._candidate_switching is False


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _panel():
    from app.ui.panels.music_panel import MusicPanel
    fake = MagicMock()
    fake.get_volume.return_value = 50
    fake.get_online_sources.return_value = []
    return MusicPanel(music=fake)


def test_on_search_show_renders_and_switches_tab(qapp):
    p = _panel()
    p._on_search_show({
        "query": "q", "generation": 1,
        "display": [
            {"idx": 0, "source": "kw", "title": "夜曲", "artist": "周杰伦"},
            {"idx": 1, "source": "wy", "title": "夜曲", "artist": "周杰伦"},
        ],
    })
    assert p.list_results.count() == 2
    assert p._voice_generation == 1
    assert p.tabs.currentIndex() == 1


def test_on_search_show_stale_generation_ignored(qapp):
    p = _panel()
    p._on_search_show({"query": "q", "generation": 5,
                       "display": [{"idx": 0, "source": "kw", "title": "A", "artist": "A"}]})
    p._on_search_show({"query": "q", "generation": 3, "display": []})
    assert p._voice_generation == 5


def test_on_play_voice_uses_resume(qapp):
    p = _panel()
    p._on_search_show({"query": "q", "generation": 7,
                       "display": [{"idx": 0, "source": "kw", "title": "A", "artist": "A"}]})
    p._music.play_online_resume.return_value = True
    p._on_play(p.list_results.item(0))
    p._music.play_online_resume.assert_called_once_with(7, 0)


def test_on_play_manual_uses_play_online(qapp):
    p = _panel()
    p._voice_generation = None
    from PySide6.QtWidgets import QListWidgetItem
    from app.music.online.models import OnlineTrack
    p._tracks_by_row = [OnlineTrack(source_name="kw", track_id="1", title="A", artist="A")]
    p.list_results.addItem(QListWidgetItem("[kw] A - A"))
    p._music.play_online.return_value = True
    p._on_play(p.list_results.item(0))
    p._music.play_online.assert_called_once()
    p._music.play_online_resume.assert_not_called()


def test_on_search_done_resets_voice_generation(qapp):
    p = _panel()
    p._voice_generation = 5
    p._on_search_done({})
    assert p._voice_generation is None
def test_play_online_before_load_callback_false(tmp_path):
    """回调返回 False -> 不 loadfile、不改 _current_song、返回 False。"""
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    prev_song = c._current_song
    ok = c.play_online(_track(), _before_load=lambda: False)
    assert ok is False
    c.mpv.loadfile.assert_not_called()
    assert c._current_song is prev_song

def test_resume_failover_no_late_loadfile_after_cancel(tmp_path):
    """用户取消后：晚到的 play_online 不再产生副作用（不 loadfile、不劫持播放）。"""
    import time as _real
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    c.mpv.get_property.return_value = False
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q", [_track()])
    gen = c._candidate_generation

    def _slow_get_url(track):
        c._cancel_candidate_switch()  # 模拟用户在网络等待期间操作
        return {"url": "http://x/b.mp3", "headers": None}
    c.online_manager.get_play_url.side_effect = _slow_get_url

    with patch("time.sleep", return_value=None):
        c.play_online_resume(gen, 0)
    for _ in range(80):
        if not c._candidate_switching:
            break
        _real.sleep(0.05)

    c.mpv.loadfile.assert_not_called()
def test_before_load_checkpoint_after_headers(tmp_path):
    """检查点紧贴 loadfile：set_property 完成后 token 变化 -> 不 loadfile、不改状态。"""
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    my_token = 7
    c._candidate_switch_token = 7
    c._candidate_switching = True
    prev_song = c._current_song

    real_set = c.mpv.set_property
    def _cancel_after_headers_set(name, *a, **kw):
        if name == "http-header-fields" and a and a[0] == "":
            c._cancel_candidate_switch()  # 模拟用户操作恰好落在 set_property 窗口
        return real_set(name, *a, **kw)
    c.mpv.set_property.side_effect = _cancel_after_headers_set

    ok = c.play_online(_track(), _before_load=lambda: my_token == c._candidate_switch_token)
    assert ok is False
    c.mpv.loadfile.assert_not_called()
    assert c._current_song is prev_song
def test_play_online_resume_restarts_from_zero(tmp_path):
    """换源重播：成功后从 0:00 开始（seek 从未被调用）。"""
    import time as _real
    c = _make_controller(tmp_path)
    c.online_manager = MagicMock()
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    c.mpv.get_property.return_value = False  # idle-active=False，播起来了
    with patch.object(c.event_bus, "publish"):
        c._publish_candidates("q", [_track()])
    gen = c._candidate_generation

    with patch("time.sleep", return_value=None):
        ok = c.play_online_resume(gen, 0)
        assert ok is True
    for _ in range(80):
        if not c._candidate_switching:
            break
        _real.sleep(0.05)

    c.mpv.loadfile.assert_called()
    c.mpv.seek.assert_not_called()
    assert c._candidate_switching is False