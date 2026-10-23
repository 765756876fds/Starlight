"""lyrics_window 跨线程歌词加载定向测试。

验证：
- 事件来自非窗口线程时，Qt 控件更新仍投递到窗口线程执行（不跨线程直操作）；
- 在线歌词网络获取在后台线程执行，不阻塞事件发布方（音乐锁内路径仅 O(1) 提交）；
- 快速连续切歌时旧请求迟到结果不覆盖新歌（请求代次过期校验）；
- 网络异常 → 占位歌词，后台任务不永久停止；
- 窗口关闭后迟到结果被丢弃，不访问已销毁的 Qt 对象；
- 本地 .lrc 与轮询（_sync）路径语义保留。

需要 QApplication（offscreen）+ Qt 事件循环 pump；不启动真实播放/UI 主程序。
"""
import threading
import time

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.ui.widgets.lyrics_window import LyricsWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def pump(qapp, n=80):
    """有界处理投递到主线程的 Queued 信号（测试驱动事件循环）。"""
    for _ in range(n):
        qapp.processEvents()
        time.sleep(0.005)


def _online_song(title="歌A", track_id="t1"):
    return {
        "title": title,
        "artist": "艺A",
        "album": "专A",
        "path": "http://x.example/a.mp3",
        "source_name": "src",
        "track_id": track_id,
        "extra": {"_raw": {"id": track_id}},
    }


class _Event:
    def __init__(self, song):
        self.data = {"song": song}


class FakeOnlineManager:
    def __init__(self):
        self.result = "[00:01.00]行1"
        self.exc = None
        self.block = None          # threading.Event：get_lyric 内等待（模拟慢网络）
        self.started = threading.Event()
        self.thread_id = None
        self.calls = []

    def get_lyric(self, track):
        self.calls.append(track)
        self.thread_id = threading.get_ident()
        self.started.set()
        if self.block is not None:
            self.block.wait()
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeController:
    def __init__(self):
        self.online_manager = FakeOnlineManager()
        self._song = None

    def get_current_song(self):
        return self._song


class _Spy(QObject):
    """QObject spy：从后台线程发射的信号，其槽必须在窗口线程执行（与生产槽同语义）。"""

    got = Signal(object, object, object)  # (seq, snapshot, lines)

    def __init__(self):
        super().__init__()
        self.thread_id = None
        self.got.connect(self._record)

    def _record(self, seq, snapshot, lines):
        self.thread_id = threading.get_ident()


# ---- 1. 跨线程事件：控件更新在主线程，网络在后台线程 ----
def test_cross_thread_event_applies_lyrics_in_window_thread(qapp):
    main_tid = threading.get_ident()
    ctrl = FakeController()
    win = LyricsWindow(ctrl)
    spy = _Spy()
    win._lyric_fetched.connect(spy.got)   # 信号-信号：与生产槽同投递语义

    thr = threading.Thread(target=win._on_track_changed, args=(_Event(_online_song()),))
    thr.start()
    thr.join(3)
    assert not thr.is_alive()             # 事件 handler 立即返回（不等待网络）
    assert not ctrl.online_manager.started.is_set()  # 网络根本未在 handler 路径上

    pump(qapp)                            # 主线程执行 _on_lyric_requested → 启动后台取词
    assert ctrl.online_manager.started.wait(3)
    assert ctrl.online_manager.thread_id != main_tid   # 网络在后台线程

    pump(qapp)                            # 取词完成 → Queued 回主线程应用
    assert spy.thread_id == main_tid      # 结果投递到主线程
    assert win._lines == [(1.0, "行1")]   # 控件状态已更新
    assert win._title == "歌A"


# ---- 2. 网络不阻塞事件发布 ----
def test_network_fetch_does_not_block_event_handler(qapp):
    ctrl = FakeController()
    gate = threading.Event()
    ctrl.online_manager.block = gate      # get_lyric 阻塞，模拟慢网络
    win = LyricsWindow(ctrl)

    thr = threading.Thread(target=win._on_track_changed, args=(_Event(_online_song()),))
    thr.start()
    thr.join(2)
    assert not thr.is_alive()             # handler 已返回，未被 get_lyric 阻塞
    assert not ctrl.online_manager.started.is_set()  # 且 get_lyric 尚未开始

    pump(qapp)
    assert ctrl.online_manager.started.wait(2)   # 取词在后台线程开始
    gate.set()                            # 放行网络
    pump(qapp)
    assert win._lines == [(1.0, "行1")]


# ---- 3. 快速连续切歌：旧请求迟到结果不覆盖新歌 ----
def test_stale_fetch_does_not_override_new_song(qapp):
    ctrl = FakeController()
    mgr = ctrl.online_manager
    win = LyricsWindow(ctrl)

    gate_a = threading.Event()
    mgr.block = gate_a                    # 歌A 网络阻塞（将迟到）
    win._on_track_changed(_Event(_online_song("歌A", "t1")))
    assert mgr.started.wait(2)

    mgr.started.clear()
    mgr.block = None
    mgr.result = "[00:02.00]歌B歌词"
    win._on_track_changed(_Event(_online_song("歌B", "t2")))  # 歌B 先完成
    pump(qapp)
    assert win._lines == [(2.0, "歌B歌词")]
    assert win._title == "歌B"

    gate_a.set()                          # 歌A 迟到结果到达
    pump(qapp)
    assert win._lines == [(2.0, "歌B歌词")]  # 未被旧结果覆盖
    assert win._title == "歌B"


# ---- 4. 网络异常：占位 + 后台任务不永久停止 ----
def test_network_exception_placeholder_and_recovers(qapp):
    ctrl = FakeController()
    mgr = ctrl.online_manager
    mgr.exc = RuntimeError("网络挂了")
    win = LyricsWindow(ctrl)

    win._on_track_changed(_Event(_online_song("歌A", "t1")))
    pump(qapp)
    assert win._lines == []               # 失败 → 占位（保留标题）
    assert win._title == "歌A"

    mgr.exc = None                        # 网络恢复
    mgr.result = "[00:03.00]歌C歌词"
    win._on_track_changed(_Event(_online_song("歌C", "t3")))
    pump(qapp)
    assert win._lines == [(3.0, "歌C歌词")]  # 后台任务未永久停止
    assert win._title == "歌C"


# ---- 5. 窗口关闭后：迟到结果丢弃，不访问已销毁控件 ----
def test_closed_window_discards_late_result(qapp):
    ctrl = FakeController()
    mgr = ctrl.online_manager
    gate = threading.Event()
    mgr.block = gate
    win = LyricsWindow(ctrl)

    win._on_track_changed(_Event(_online_song("歌A", "t1")))
    assert mgr.started.wait(2)

    win.close()                           # closeEvent → stop + _closed=True
    gate.set()                            # 迟到结果到达
    pump(qapp)
    assert win._lines == []               # 结果被丢弃（worker 未 emit 到已销毁对象）
    assert win._title == ""


# ---- 6. 无 online_manager：占位 ----
def test_no_online_manager_placeholder(qapp):
    ctrl = FakeController()
    ctrl.online_manager = None
    win = LyricsWindow(ctrl)

    win._on_track_changed(_Event(_online_song("歌A", "t1")))
    pump(qapp)
    assert win._lines == []
    assert win._title == "歌A"


# ---- 7. 本地 .lrc：主线程同步路径保留 ----
def test_local_lyric_sync_flow(qapp, tmp_path):
    ctrl = FakeController()
    win = LyricsWindow(ctrl)
    (tmp_path / "song.lrc").write_text("[00:01.50]本地行", encoding="utf-8")
    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"x")

    win._request_lyric({"title": "本地歌", "path": str(mp3)})
    # 主线程 Direct 同步：本地读 + 应用立即完成
    assert win._lines == [(1.5, "本地行")]
    assert win._title == "本地歌"


# ---- 8. 轮询（_sync）检测换歌：语义保留 ----
def test_sync_poll_detects_new_song(qapp):
    ctrl = FakeController()
    win = LyricsWindow(ctrl)
    ctrl._song = _online_song("歌A", "t1")

    win._sync()                           # 主线程轮询 tick
    pump(qapp)
    assert win._lines == [(1.0, "行1")]
    assert win._title == "歌A"
