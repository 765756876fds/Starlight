"""电影内嵌 S8：mpv 崩溃自愈测试（offscreen）。

覆盖：
- MovieController.is_crashed 判定（进程死 + 有上下文）
- restart_current 恢复链（stop_process → start(wid) → loadfile → seek 续播）
- 防重入 + 3 次/5 分钟上限 + 窗口重置
- 关窗中断（worker 守卫放弃恢复 + 回收进程）
- VideoPlayerWindow._tick 状态机（检测→提示→轮询恢复→成功/放弃收尾）
- 回归：正常播放/正常停播不触发恢复
"""
import pytest

pytestmark = pytest.mark.ui

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from app.movie.movie_controller import MovieController

# patch 的是 movie_controller.threading.Thread 属性——测试内要起真线程时
# 必须用 patch 前保存的引用（否则递归进 mock）。
_REAL_THREAD = threading.Thread


# ================= Controller 层（真 controller + mock mpv） =================

def _mk_ctrl():
    """真 MovieController + mock MpvController（构造即返回 mock 实例）。"""
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(source_url="http://source.test/api", config=None)
        # 模拟真实 mpv JSON IPC 成功响应；MagicMock 不是有效协议响应。
        M.return_value.loadfile.return_value = {"error": "success"}
        return ctrl, M.return_value


_ITEM = {
    "vod_name": "测试片",
    "play_url": "A$http://u.test/1.m3u8#B$http://u.test/2.m3u8",
    "first_url": "http://u.test/1.m3u8",
}


class _SyncThread:
    """patch threading.Thread 的同步壳：start() 立即执行 target（不真起线程）。"""

    def __init__(self, target, args=(), kwargs=None, **_):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _sync_thread(patch_thread):
    """把 controller 里的 threading.Thread 变同步（worker 立即执行）——
    daemon 线程异步会让测试不稳定，同步化后断言立即可见。"""
    patch_thread.side_effect = _SyncThread


def _capture_thread(patch_thread, captured):
    """真实 daemon 线程但捕获句柄（测异步场景：关窗中断）。"""
    def _spawn(target, args=(), kwargs=None, daemon=True, **_):
        t = _REAL_THREAD(target=target, args=args,
                         kwargs=kwargs or {}, daemon=daemon)
        captured["t"] = t
        return t
    patch_thread.side_effect = _spawn


class TestControllerRecover:
    def test_is_crashed_true_when_process_dead_with_context(self):
        """mpv 进程死 + _current 有上下文 → 崩溃（可触发自愈）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        ctrl._current = dict(_ITEM)
        assert ctrl.is_crashed() is True

    def test_is_crashed_false_when_process_alive(self):
        """mpv 活着（正常播放/停播）→ 非崩溃。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = True
        ctrl._current = dict(_ITEM)
        assert ctrl.is_crashed() is False

    def test_is_crashed_false_when_no_context(self):
        """播完末集/用户停止（_current 已清）→ 非崩溃（即使进程死——idle 无碍）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        ctrl._current = None
        assert ctrl.is_crashed() is False

    def test_restart_rebuilds_full_chain(self):
        """崩溃恢复链：stop_process → start(taskkill=False, wid=重传) →
        loadfile 当前集 URL → seek 回崩溃前位置（续播）+ 重置连播基线。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        mpv.get_live_playback_time.return_value = 42.0
        ctrl._current = dict(_ITEM)
        ctrl._current_index = 1      # 第 2 集 URL
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _sync_thread(T)
            ok = ctrl.restart_current(wid=0x1234)
        assert ok is True
        mpv.stop_process.assert_called_once()
        mpv.start.assert_called_once()
        args = mpv.start.call_args
        assert args.kwargs.get("wid") == 0x1234      # --wid 重传
        assert args.kwargs.get("taskkill") is False  # 不杀音乐实例
        mpv.loadfile.assert_called_once_with("http://u.test/2.m3u8")
        mpv.seek.assert_called_once_with(42.0, "absolute")
        assert ctrl._last_idle is False              # 连播基线重置

    def test_restart_uses_embed_wid_when_not_passed(self):
        """窗口不显式传 wid → 用 controller 已注入的 _embed_wid。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        ctrl._embed_wid = 0x5678
        ctrl._current = dict(_ITEM)
        ctrl._current_index = 0
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _sync_thread(T)
            ctrl.restart_current()
        assert mpv.start.call_args.kwargs.get("wid") == 0x5678

    def test_restart_no_reentry_while_pending(self):
        """防重入：一次恢复进行中 → 重复调用不新起（返回 True 但不再 start）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        ctrl._current = dict(_ITEM)
        ctrl._recover_pending = True     # 模拟第一次恢复还没跑完
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _sync_thread(T)
            ok = ctrl.restart_current(wid=1)
        assert ok is True
        assert mpv.start.call_count == 0     # 第二个线程没起

    def test_restart_gives_up_after_3_failures(self):
        """上限 3 次/5 分钟：重启失败 3 次后第 4 次返回 False + 清上下文 + gave_up。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = False       # 重启总失败（坏源持续崩）
        for _ in range(3):
            ctrl._current = dict(_ITEM)
            with patch("app.movie.movie_controller.threading.Thread") as T:
                _sync_thread(T)
                ok = ctrl.restart_current(wid=1)
            assert ok is True                # 前 3 次仍调度
            assert ctrl._recover_pending is False   # worker 已清
        assert ctrl._recover_count == 3
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _sync_thread(T)
            ok = ctrl.restart_current(wid=1)  # 第 4 次（无 _current——上次失败未清）
        assert ok is False
        assert ctrl.is_gave_up() is True
        assert ctrl._current is None          # 放弃即清上下文（is_crashed → False）
        assert mpv.start.call_count == 3      # 第 4 次不再尝试

    def test_restart_window_resets_after_5min(self):
        """窗口重置：距上次窗口起点超 300s → 计数清零可再恢复。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        ctrl._recover_count = 3
        ctrl._recover_window_ts = time.time() - 400   # 过期窗口
        ctrl._current = dict(_ITEM)
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _sync_thread(T)
            ok = ctrl.restart_current(wid=1)
        assert ok is True
        assert ctrl._recover_count == 1       # 窗口重置后重新计数

    def test_restart_aborts_when_user_closed(self):
        """关窗中断：恢复完成时用户已停止（_current=None）→ 不 loadfile +
        回收刚起的进程（不留孤儿）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.get_live_playback_time.return_value = 42.0
        started = threading.Event()

        def _slow_start(*_a, **_k):
            # 模拟重启耗时：保证主线程在 worker 守卫检查前设好 _current=None
            started.set()
            time.sleep(0.2)
            return True

        mpv.start.side_effect = _slow_start
        ctrl._current = dict(_ITEM)
        ctrl._current_index = 0
        captured = {}
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _capture_thread(T, captured)
            ctrl.restart_current(wid=1)
            started.wait(timeout=5)   # worker 已开始重启（阻塞在 start）
            ctrl._current = None      # 模拟用户关窗（controller.stop）
        captured["t"].join(timeout=5)
        mpv.start.assert_called_once()        # 进程已起
        assert mpv.loadfile.call_count == 0   # 守卫放弃恢复
        assert mpv.stop_process.call_count >= 2   # 恢复前 1 次 + 回收 1 次

    def test_restart_returns_false_when_no_current(self):
        """无可恢复上下文（_current=None）→ 返回 False（不调度）。"""
        ctrl, mpv = _mk_ctrl()
        ctrl._current = None
        with patch("app.movie.movie_controller.threading.Thread") as T:
            _sync_thread(T)
            ok = ctrl.restart_current(wid=1)
        assert ok is False
        assert mpv.start.call_count == 0


# ================= 窗口层（VideoPlayerWindow + 可崩 FakeCtl） =================

class _RecoverCtl:
    """窗口层 mock 控制器：播放/崩溃/放弃状态可控。"""

    def __init__(self):
        self.playing = True
        self.crashed = False
        self.gave_up = False
        self.restart_calls = []
        self.stopped = False

    def tick(self):
        pass

    def is_playing(self):
        return self.playing

    def is_crashed(self):
        return self.crashed

    def is_gave_up(self):
        return self.gave_up

    def restart_current(self, wid=None):
        self.restart_calls.append(wid)
        if self.gave_up:
            self.crashed = False
            self.playing = False
            return False
        self.crashed = False
        self.playing = True    # 恢复成功（下一 tick 可见）
        return True

    def get_position(self):
        return 0.0

    def get_duration(self):
        return 0.0

    def current_item(self):
        return None

    def set_embed_wid(self, wid):
        pass

    def play(self, item, index=None):
        return True

    def stop(self):
        self.stopped = True
        self.playing = False


def _mk_window(ctl=None):
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    ctl = ctl or _RecoverCtl()
    w = VideoPlayerWindow(ctl)
    return w, ctl


class TestWindowRecover:
    def test_tick_detects_crash_starts_recover(self):
        """崩溃（进程死+播放态）→ _tick 触发自愈：提示 label + restart 被调 + 防重入置位。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = True
        ctl.playing = False
        with patch.object(w, "_apply_mpv_mouse_passthrough"):
            w._tick()
        assert w._recovering is True
        assert ctl.restart_calls == [w.container.native_id()]   # wid 从容器取
        assert not w._recover_label.isHidden()                  # offscreen：父窗未 show，用 isHidden
        assert "正在恢复" in w._recover_label.text()
        assert ctl.stopped is False

    def test_tick_recover_success_restores(self):
        """恢复成功（下一 tick is_playing 回 True）→ 隐藏提示 + 穿透重设 + 回正常轮询。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = True
        ctl.playing = False
        w._start_recover()                 # 触发（内部 restart → fake 恢复成功）
        assert w._recovering is True
        w._tick()                          # 下一 tick：is_playing True
        assert w._recovering is False
        assert w._recover_label.isHidden()

    def test_tick_recover_gave_up_stops(self):
        """超限放弃 → 失败提示 + _on_stopped 收尾（不再重启）。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = True
        ctl.playing = False
        ctl.gave_up = True                 # restart_current 返回 False + 清态
        w._start_recover()
        assert w._recovering is False
        assert not w._recover_label.isHidden()
        assert "恢复失败" in w._recover_label.text()
        assert w._playing is False         # 已收尾

    def test_tick_no_reentry_while_recovering(self):
        """恢复中防重入：_recovering 期间重复 _tick 不再调 restart。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = True
        ctl.playing = False
        w._start_recover()
        calls = len(ctl.restart_calls)
        ctl.crashed = True                 # 恢复中又检测到崩（不应再触发）
        w._tick()
        assert len(ctl.restart_calls) == calls

    def test_close_during_recover_stops_it(self):
        """恢复中关窗 → 停止恢复（controller.stop 清上下文，worker 放弃）+ 隐藏提示。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = True
        ctl.playing = False
        w._start_recover()
        assert w._recovering is True
        w.close()
        assert w._recovering is False
        assert w._recover_label.isHidden()
        assert ctl.stopped is True

    def test_tick_normal_play_no_recover(self):
        """回归：正常播放（is_playing True）→ 正常轮询，不触发恢复。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = False
        ctl.playing = True
        w._tick()
        assert w._recovering is False
        assert ctl.restart_calls == []
        assert w._playing is True

    def test_tick_normal_stop_no_recover(self):
        """回归：正常停播（播完/用户停止，进程活着非崩溃）→ 原收尾，不触发恢复。"""
        w, ctl = _mk_window()
        w._playing = True
        ctl.crashed = False
        ctl.playing = False
        w._tick()
        assert w._recovering is False
        assert ctl.restart_calls == []
        assert w._playing is False         # _on_stopped 收尾
