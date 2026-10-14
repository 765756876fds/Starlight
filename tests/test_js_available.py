"""修3：js 状态误报 ok → JSMusicSource.available 判定（node 缺失/未就绪 → False）。"""
import threading

from app.music.online.sources.js_source import JSMusicSource


def _make_js(proc=None, ready=False):
    src = JSMusicSource.__new__(JSMusicSource)
    src._proc = proc
    src._ready = threading.Event()
    if ready:
        src._ready.set()
    return src


class TestJsAvailable:
    def test_false_when_no_proc(self):
        """node 缺失（_proc=None）→ available=False。"""
        src = _make_js(proc=None, ready=False)
        assert src.available is False

    def test_false_when_not_ready(self):
        """进程在但 3s 未就绪（_ready 未 set）→ available=False。"""
        src = _make_js(proc=object(), ready=False)
        assert src.available is False

    def test_true_when_proc_and_ready(self):
        """进程存活且 ready 已就绪 → available=True。"""
        src = _make_js(proc=object(), ready=True)
        assert src.available is True

    def test_false_on_internal_error(self):
        """内部异常 → 兜底返回 False（不冒泡）。"""
        src = JSMusicSource.__new__(JSMusicSource)
        src._proc = None
        src.__dict__.pop("_ready", None)  # 模拟属性缺失的异常路径
        assert src.available is False
