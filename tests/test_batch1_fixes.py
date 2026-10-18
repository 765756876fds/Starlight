"""批次1（UI 控件与交互）回归：
弹窗全中文 primary / 播放按钮锁定不闪回 / 电影外部进度条隐藏 / 波形深灰底座。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------- ① 自动禁用/自动选择弹窗：全中文 + primary ----------------
def test_banpick_dialog_chinese_buttons(qapp):
    from PySide6.QtWidgets import QPushButton
    from app.ui.dialogs.ban_pick_config_dialog import BanPickConfigDialog
    dlg = BanPickConfigDialog("自动禁用配置", [1, 2])
    texts = [b.text() for b in dlg.findChildren(QPushButton)]
    assert "确定" in texts and "取消" in texts, f"按钮未中文化: {texts}"
    ok = next(b for b in dlg.findChildren(QPushButton) if b.text() == "确定")
    assert ok.property("class") == "primary", "确定按钮必须挂 primary"


# ---------------- ② 播放按钮锁定：mpv 异步期间图标不闪回 ----------------
class SlowMusic:
    """toggle 后 300ms 才真正翻转 is_playing（模拟 mpv 异步启动）。"""
    def __init__(self):
        self._playing = False
    def is_playing(self):
        return self._playing
    def toggle(self):
        QTimer.singleShot(300, lambda: setattr(self, "_playing", True))
    def get_play_mode(self): return "sequential"
    def is_highlight_mode(self): return False
    def get_volume(self): return 70
    def get_online_sources(self): return []
    def search_local(self, kw=""): return []
    def get_current_song(self): return None


def test_toggle_icon_locked_no_flashback(qapp):
    from app.ui.panels.music_panel import MusicPanel
    slow = SlowMusic()
    panel = MusicPanel(music=slow)
    try:
        panel._sync_toggle_icon()
        assert panel._toggle_icon == "play"
        panel._on_toggle()                    # 点击：立即切 ⏸ 并锁定
        assert panel._toggle_icon == "pause"
        # 锁定期间（is_playing 仍 False，旧 150ms 校正会闪回）图标必须保持
        panel._sync_toggle_icon()
        assert panel._toggle_icon == "pause", "锁定期间图标被旧状态闪回"
        QTest.qWait(600)                      # 推进事件循环：真实翻转 + 轮询确认
        assert slow.is_playing() is True
        assert panel._toggle_icon == "pause", "真实播放后图标应保持暂停"
    finally:
        panel._timer.stop()


def test_toggle_icon_unlocks_on_failed_switch(qapp):
    """切换未生效（mpv 不可用）→ 轮询结束解锁并按真实状态校正回 play。"""
    class FailMusic:
        def is_playing(self): return False
        def toggle(self): pass
        def get_play_mode(self): return "sequential"
        def is_highlight_mode(self): return False
        def get_volume(self): return 70
        def get_online_sources(self): return []
        def search_local(self, kw=""): return []
        def get_current_song(self): return None
    from app.ui.panels.music_panel import MusicPanel
    panel = MusicPanel(music=FailMusic())
    try:
        panel._on_toggle()
        assert panel._toggle_icon == "pause"
        QTest.qWait(3800)                     # 超过轮询上限（15×200ms）→ 解锁校正
        assert panel._icon_locked is False
        assert panel._toggle_icon == "play", "切换失败应校正回 play"
    finally:
        panel._timer.stop()


# ---------------- ③ 电影面板：外部进度条隐藏 ----------------
class _FakeMovie:
    def embed_enabled(self): return True
    def set_embed_wid(self, wid): pass
    def is_playing(self): return False
    def toggle_pause(self): pass
    def stop(self): pass
    def get_position(self): return 0
    def get_duration(self): return 0
    def current_item(self): return None
    def is_auto_next(self): return False
    def set_auto_next(self, enabled): pass
    def get_volume(self): return 50
    def set_volume(self, v): pass
    def is_muted(self): return False
    def set_muted(self, m): pass
    def search(self, kw): return []
    def sources(self): return []
    def set_source(self, s): pass
    def get_source(self): return ""
    def play(self, item, index=None): return False


def test_movie_panel_progress_slider_hidden(qapp):
    from app.ui.panels.movie_panel import MoviePanel
    panel = MoviePanel(movie=_FakeMovie())
    try:
        assert panel.slider_pos is not None
        assert panel.slider_pos.isHidden(), "外部进度条必须从界面隐藏"
    finally:
        panel._timer.stop()
        if panel._video_win is not None:
            panel._video_win._timer.stop()


# ---------------- ④ 波形进度条：深灰底座 + 播放态渲染不崩 ----------------
def test_waveform_slider_renders(qapp):
    from app.ui.widgets.waveform_slider import WaveformSlider
    wv = WaveformSlider()
    wv.resize(520, 90)
    wv.setRange(0, 1000)
    wv.setValue(400)
    wv.set_active(True)
    for _ in range(5):
        wv._step()
    img = wv.grab().toImage()
    assert not img.isNull(), "波形进度条离屏渲染失败"
