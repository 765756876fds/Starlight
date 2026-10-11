"""UI 自定义音源（HTTP JSON）与同步注册逻辑测试。"""
import os
import pytest
from unittest.mock import MagicMock, patch

import requests

from app.music.online.models import OnlineTrack
from app.music.online.sources.http_source import HttpJsonSource, SourceError
from app.music.online.manager import OnlineSourceManager


@pytest.fixture()
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


# ---------- HttpJsonSource ----------

class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)
        return None

    def json(self):
        return self._payload


def _src():
    return HttpJsonSource(name="ikun", api_url="https://api.example.com")


def test_http_source_search_parse_lx_style():
    """洛雪风格 {data:[...]} 响应解析。"""
    src = _src()
    payload = {"data": [
        {"name": "稻香", "artist": "周杰伦", "id": "1001", "duration": 253},
        {"name": "晴天", "singer": "周杰伦", "songId": "1002", "interval": 269},
    ]}
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp(payload)) as m:
        tracks = src.search("周杰伦", limit=5)
    m.assert_called_once()
    assert len(tracks) == 2
    assert tracks[0].title == "稻香" and tracks[0].artist == "周杰伦"
    assert tracks[0].track_id == "1001"
    assert tracks[0].source_name == "ikun"
    assert tracks[1].track_id == "1002"


def test_extract_track_album_dict_artist_variants():
    """album 为字典、artist 为字符串/字典/列表时均正确解析。"""
    src = _src()
    payload = {"data": [
        {"name": "A", "artist": "某人", "id": "1",
         "album": {"name": "专辑甲"}},
        {"name": "B", "artist": {"name": "组合乙"}, "id": "2",
         "al": "旧专辑"},
        {"name": "C", "artist": ["成员一", {"name": "成员二"}], "id": "3",
         "album": "专辑丙"},
        {"name": "D", "artist": "歌者", "id": "4", "duration": "不是数字"},
    ]}
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp(payload)):
        tracks = src.search("x", limit=10)
    assert [t.album for t in tracks] == ["专辑甲", "旧专辑", "专辑丙", ""]
    assert tracks[0].artist == "某人"
    assert tracks[1].artist == "组合乙"
    assert tracks[2].artist == "成员一、成员二"
    assert tracks[3].duration_seconds == 0  # 非法 duration 不炸


def test_http_source_error_categories():
    """网络/HTTP/JSON/结构错误分别归类，空结果为真空（无错误）。"""
    src = _src()
    # 网络异常
    with patch("app.music.online.sources.http_source.requests.get",
               side_effect=requests.ConnectionError("conn")):
        with pytest.raises(SourceError) as e1:
            src.search("歌")
        assert e1.value.category == "network"
    # 超时
    with patch("app.music.online.sources.http_source.requests.get",
               side_effect=requests.Timeout("t")):
        with pytest.raises(SourceError) as e2:
            src.search("歌")
        assert e2.value.category == "network_timeout"
    # HTTP 状态错误
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp({}, status_code=500)):
        with pytest.raises(SourceError) as e3:
            src.search("歌")
        assert e3.value.category == "http_500"
    # JSON 解析错误
    bad = FakeResp(b"not-json")
    bad.json = MagicMock(side_effect=ValueError("bad json"))
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=bad):
        with pytest.raises(SourceError) as e4:
            src.search("歌")
        assert e4.value.category == "json"
    # 结构不兼容（非预期顶层）
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp({"unexpected": True})):
        with pytest.raises(SourceError) as e5:
            src.search("歌")
        assert e5.value.category == "structure"
    # 有数据但全部提取失败 → structure
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp({"data": [{"foo": "bar"}]})):
        with pytest.raises(SourceError) as e6:
            src.search("歌")
        assert e6.value.category == "structure"
    # 真正无搜索结果 → 空列表，无异常
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp({"data": []})):
        assert src.search("不存在") == []


def test_http_source_error_recorded_by_manager():
    """单源失败 → manager._errors 记录分类，不影响其他源结果。"""
    m = OnlineSourceManager()

    class GoodSource(HttpJsonSource):
        def search(self, keyword, limit=20):
            return [OnlineTrack(source_name="good", track_id="1", title="OK")]

    m.register(GoodSource(name="good", api_url="https://g.example.com"))
    m.register(HttpJsonSource(name="bad", api_url="https://b.example.com"))
    with patch("app.music.online.sources.http_source.requests.get",
               side_effect=requests.ConnectionError("down")):
        out = m.search_all("歌")
    assert out["good"] and out["good"][0].title == "OK"
    assert out["bad"] == []
    assert "network" in m._errors.get("bad", "")


def test_http_source_no_api_url_safe():
    """api_url 为空（如仅存元信息的 JS 导入）→ search 空、get_play_url None。"""
    src = HttpJsonSource(name="lx-玉宁熙", api_url="")
    assert src.search("歌") == []
    t = OnlineTrack(source_name="x", track_id="1", title="t")
    assert src.get_play_url(t) is None


def test_http_source_get_play_url():
    src = _src()
    t = OnlineTrack(source_name="ikun", track_id="1001", title="稻香")
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp({"url": "https://cdn.example.com/a.mp3"})) as m:
        url = src.get_play_url(t)
    assert url == "https://cdn.example.com/a.mp3"
    assert m.call_args.kwargs["params"] == {"id": "1001"}
    with patch("app.music.online.sources.http_source.requests.get",
               return_value=FakeResp({})):
        assert src.get_play_url(t) is None


# ---------- sync_custom_sources ----------

def _fake_config_manager(sources):
    """构造 fake MusicSourceManager，返回给定源列表。"""
    fm = MagicMock()
    fm.get_all_sources.return_value = sources
    return fm


def _make_source(name, api_url, enabled=True, search_path="/search",
                 song_url_path="/song/url"):
    from app.music.source_manager import MusicSource
    return MusicSource(name=name, api_url=api_url, enabled=enabled,
                       search_path=search_path, song_url_path=song_url_path)


def _ctrl(tmp_path):
    from app.music.controller import MusicController
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / "sync.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": "\\\\.\\pipe\\test",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    return ctrl, db


def test_sync_custom_sources_registers_enabled(tmp_path):
    ctrl, db = _ctrl(tmp_path)
    try:
        ctrl.online_manager = OnlineSourceManager()
        fm = _fake_config_manager([
            _make_source("ikun", "https://api.ikunshare.com"),
            _make_source("disabled", "https://api.x.com", enabled=False),
            _make_source("empty", ""),
        ])
        with patch("app.music.source_manager.MusicSourceManager", return_value=fm):
            n = ctrl.sync_custom_sources()
        assert n == 1  # 只注册启用且 api_url 非空
        assert "ikun" in ctrl.online_manager.list_sources()
        assert "disabled" not in ctrl.online_manager.list_sources()
        assert "empty" not in ctrl.online_manager.list_sources()
    finally:
        db.close()


def test_sync_custom_sources_does_not_override_builtin(tmp_path):
    ctrl, db = _ctrl(tmp_path)
    try:
        mgr = OnlineSourceManager()

        class Builtin(MagicMock):
            name = "js_bridge"
            def search(self, kw, limit=20): return []
            def get_play_url(self, t): return None
            def get_lyric(self, t): return None
            def close(self): pass

        mgr.register(Builtin())
        ctrl.online_manager = mgr
        fm = _fake_config_manager([
            _make_source("js_bridge", "https://evil.example.com"),
        ])
        with patch("app.music.source_manager.MusicSourceManager", return_value=fm):
            n = ctrl.sync_custom_sources()
        assert n == 0  # 重名被跳过
        assert ctrl.online_manager._sources["js_bridge"].name == "js_bridge"
    finally:
        db.close()


def test_sync_custom_sources_idempotent(tmp_path):
    ctrl, db = _ctrl(tmp_path)
    try:
        ctrl.online_manager = OnlineSourceManager()
        fm = _fake_config_manager([_make_source("ikun", "https://api.ikunshare.com")])
        with patch("app.music.source_manager.MusicSourceManager", return_value=fm):
            assert ctrl.sync_custom_sources() == 1
            assert ctrl.sync_custom_sources() == 1
        assert ctrl.online_manager.list_sources().count("ikun") == 1
    finally:
        db.close()


# ---------- 对话框回调 / 信号 ----------

def test_source_dialog_notify_changed(qapp):
    """对话框音源变更后触发同步回调（不弹 UI）。"""
    from app.ui.dialogs.source_manage_dialog import SourceManageDialog
    calls = []
    dlg = SourceManageDialog(parent=None, on_sources_changed=lambda: calls.append(1))
    dlg._notify_changed()
    assert calls == [1]
    dlg.close()


def test_http_source_url_join_full_path():
    """URL 拼接：普通 path 拼在 api_url 后；path 为完整 URL 时直接使用。"""
    src = HttpJsonSource(name="x", api_url="https://api.example.com")
    assert src._join("/search") == "https://api.example.com/search"
    assert src._join("https://other.example.com/full") == "https://other.example.com/full"
    # api_url 带尾斜杠会被 rstrip，不会出现双斜杠
    src2 = HttpJsonSource(name="y", api_url="https://api.example.com/")
    assert src2._join("/search") == "https://api.example.com/search"


def test_sync_js_source_with_api_url_registers(tmp_path):
    """source_type=js 但 api_url 非空且启用 → 按 HTTP JSON 协议注册（ikun 兼容）；
    js 且 api_url 为空 → 跳过。"""
    ctrl, db = _ctrl(tmp_path)
    try:
        ctrl.online_manager = OnlineSourceManager()
        js_ok = _make_source("ikun音源", "https://api.example.com", enabled=True)
        js_ok.source_type = "js"
        js_empty = _make_source("lx-玉宁熙", "", enabled=True)
        js_empty.source_type = "js"
        fm = _fake_config_manager([js_ok, js_empty])
        with patch("app.music.source_manager.MusicSourceManager", return_value=fm):
            n = ctrl.sync_custom_sources()
        assert n == 1
        assert "ikun音源" in ctrl.online_manager.list_sources()
        assert "lx-玉宁熙" not in ctrl.online_manager.list_sources()
    finally:
        db.close()


def test_mpv_header_reset_across_sources(tmp_path):
    """酷我源设置带 Referer 的 header；非酷我源重置为无 Referer，避免跨源残留。"""
    from app.music.online.manager import OnlineSourceManager
    from app.music.online.source import MusicSource
    from app.music.online.models import OnlineTrack

    class KwSource(MusicSource):
        name = "kw-kuwo"
        def search(self, keyword, limit=20):
            if keyword == "稻香":
                return [OnlineTrack(source_name="kw-kuwo", track_id="1", title="稻香")]
            return []
        def get_play_url(self, track): return "https://kuwo.example.com/a.mp3"
        def get_lyric(self, track): return None

    class HttpSource(MusicSource):
        name = "ikun"
        def search(self, keyword, limit=20):
            if keyword == "歌":
                return [OnlineTrack(source_name="ikun", track_id="2", title="歌")]
            return []
        def get_play_url(self, track): return "https://ikun.example.com/b.mp3"
        def get_lyric(self, track): return None

    ctrl, db = _ctrl(tmp_path)
    try:
        ctrl.online_manager = OnlineSourceManager()
        ctrl.online_manager.register(KwSource())
        ctrl.play_search_result("稻香")
        kw_calls = [c for c in ctrl.mpv.set_property.call_args_list
                    if c.args[0] == "http-header-fields"]
        assert kw_calls and "Referer" in kw_calls[-1].args[1]
        assert "kuwo" in kw_calls[-1].args[1]

        ctrl.online_manager.register(HttpSource())
        ctrl.play_search_result("歌")
        all_calls = [c for c in ctrl.mpv.set_property.call_args_list
                     if c.args[0] == "http-header-fields"]
        assert all_calls and "Referer" not in all_calls[-1].args[1]
    finally:
        db.close()


def test_play_online_header_two_directions(tmp_path):
    """play_online 路径：酷我 → 设置 Referer；随后非酷我 → 重置为无 Referer；
    再切回酷我 → Referer 正确恢复（两个方向均无跨源残留）。"""
    from app.music.online.manager import OnlineSourceManager
    from app.music.online.source import MusicSource
    from app.music.online.models import OnlineTrack

    class KwUrlSource(MusicSource):
        name = "kw-kuwo"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track): return "https://kw.example.com/a.mp3"
        def get_lyric(self, track): return None

    class HttpUrlSource(MusicSource):
        name = "ikun"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track): return "https://ikun.example.com/b.mp3"
        def get_lyric(self, track): return None

    ctrl, db = _ctrl(tmp_path)
    try:
        ctrl.online_manager = OnlineSourceManager()
        ctrl.online_manager.register(KwUrlSource())
        ctrl.online_manager.register(HttpUrlSource())

        kw_track = OnlineTrack(source_name="kw-kuwo", track_id="1", title="稻香")
        assert ctrl.play_online(kw_track) is True
        headers = [c for c in ctrl.mpv.set_property.call_args_list
                   if c.args[0] == "http-header-fields"]
        assert headers and "Referer" in headers[-1].args[1]

        # 酷我 → 非酷我：Referer 必须被清除
        ikun_track = OnlineTrack(source_name="ikun", track_id="2", title="歌")
        assert ctrl.play_online(ikun_track) is True
        headers = [c for c in ctrl.mpv.set_property.call_args_list
                   if c.args[0] == "http-header-fields"]
        assert headers and "Referer" not in headers[-1].args[1]

        # 非酷我 → 酷我：Referer 正确恢复
        assert ctrl.play_online(kw_track) is True
        headers = [c for c in ctrl.mpv.set_property.call_args_list
                   if c.args[0] == "http-header-fields"]
        assert headers and "Referer" in headers[-1].args[1]
    finally:
        db.close()


def test_dialog_item_changed_single_connection(qapp, tmp_path, monkeypatch):
    """itemChanged 只连接一次；多次刷新不叠加回调；刷新过程不误触发。"""
    from PySide6.QtCore import Qt
    from app.ui.dialogs.source_manage_dialog import SourceManageDialog
    from app.music.source_manager import MusicSource

    class FakeMgr:
        def __init__(self):
            self.sources = [MusicSource(name="a", api_url="https://x.example.com",
                                        enabled=True)]
            self.enable_calls = []

        def get_all_sources(self):
            return list(self.sources)

        def enable_source(self, name, enabled):
            self.enable_calls.append((name, enabled))

        def remove_source(self, name):
            self.sources = [s for s in self.sources if s.name != name]

    fm = FakeMgr()
    monkeypatch.setattr("app.ui.dialogs.source_manage_dialog.MusicSourceManager",
                        lambda: fm)
    sync_calls = []
    dlg = SourceManageDialog(parent=None,
                             on_sources_changed=lambda: sync_calls.append(1))
    try:
        # 多次刷新（模拟导入/删除后列表重建）
        dlg._refresh_list()
        dlg._refresh_list()
        # 刷新本身不应触发 enable_source
        assert fm.enable_calls == []

        # 模拟用户勾选（刷新之外的真实操作）
        item = dlg.list_widget.item(0)
        item.setCheckState(Qt.Unchecked)
        assert len(fm.enable_calls) == 1  # 只触发一次，而非 3 次
        assert len(sync_calls) == 1
    finally:
        dlg.close()
