"""mpv _launch() 失败路径孤儿进程清理 定向测试。

根因：_launch() 的 except 分支在 Popen 成功后直接把 _process 置 None，
进程对象被丢弃且未 terminate/kill → 启动失败的 mpv 进程成为孤儿（无句柄可清理）。

本修：失败路径保留进程对象，_cleanup_failed_launch() 执行
terminate() → 有界 wait(3) → 仍存活则 kill()，清理自身异常不阻断其余步骤，
完成后统一复位 _process/_pipe/_running；Popen 本身失败时安全返回 False。

断言基于 Fake 进程的方法调用与最终状态，不依赖打印日志文本。
"""
from types import SimpleNamespace
import subprocess

import pytest

pytestmark = pytest.mark.unit

from app.music.mpv import MpvController


class FakeProc:
    """可编排的 Popen 替身：默认 terminate 即退出；stubborn 拒绝退出直到 kill。"""

    def __init__(self, stubborn=False, terminate_raises=False, kill_raises=False):
        self._stubborn = stubborn
        self._terminate_raises = terminate_raises
        self._kill_raises = kill_raises
        self._dead = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []  # 每次 wait 的 timeout 参数（验证有界等待）
        self.pid = 12345

    def poll(self):
        return -9 if self._dead else None

    def terminate(self):
        self.terminate_calls += 1
        if self._terminate_raises:
            raise OSError("terminate boom")
        if not self._stubborn:
            self._dead = True

    def kill(self):
        self.kill_calls += 1
        if self._kill_raises:
            raise OSError("kill boom")
        self._dead = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if not self._dead:
            raise subprocess.TimeoutExpired("fake-mpv", timeout)
        return 0


def _make_ctrl(monkeypatch, proc=None, wait_pipe_raises=None):
    monkeypatch.setattr("app.music.mpv.subprocess.Popen",
                        lambda *a, **k: proc)
    ctrl = MpvController(mpv_path="mpv.exe")
    if wait_pipe_raises is not None:
        monkeypatch.setattr(MpvController, "_wait_for_pipe",
                            lambda self, timeout: _raise(wait_pipe_raises))
    return ctrl


def _raise(exc):
    raise exc


def test_pipe_timeout_terminates_and_resets(monkeypatch):
    """Popen 成功 + 管道等待超时：进程被 terminate、有界 wait、状态复位、返回 False。"""
    proc = FakeProc()
    ctrl = _make_ctrl(monkeypatch, proc=proc,
                      wait_pipe_raises=TimeoutError("pipe timeout"))
    assert ctrl._launch() is False
    assert proc.terminate_calls == 1
    assert proc.wait_calls == [3], f"应执行一次有界 wait(3): {proc.wait_calls}"
    assert proc.kill_calls == 0, "terminate 即退出时不应触发 kill"
    assert ctrl._process is None
    assert ctrl._pipe is None
    assert ctrl._running is False


def test_terminate_stubborn_falls_back_to_kill(monkeypatch):
    """terminate 后仍未退出：触发 kill 降级路径，两次有界等待。"""
    proc = FakeProc(stubborn=True)
    ctrl = _make_ctrl(monkeypatch, proc=proc,
                      wait_pipe_raises=TimeoutError("pipe timeout"))
    assert ctrl._launch() is False
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1, "拒绝退出时应降级 kill"
    assert proc.wait_calls == [3, 3], f"terminate 与 kill 各一次有界等待: {proc.wait_calls}"
    assert ctrl._process is None
    assert ctrl._running is False


def test_pipe_open_failure_cleans_up(monkeypatch):
    """IPC 管道存在但连接失败（ConnectionError）：同样清理进程并复位。"""
    proc = FakeProc()
    ctrl = _make_ctrl(monkeypatch, proc=proc,
                      wait_pipe_raises=ConnectionError("pipe open failed"))
    assert ctrl._launch() is False
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 0
    assert ctrl._process is None
    assert ctrl._pipe is None
    assert ctrl._running is False


def test_popen_raises_returns_false_safely(monkeypatch):
    """Popen 本身抛异常：安全返回 False，不误调用不存在的进程清理方法。"""
    monkeypatch.setattr("app.music.mpv.subprocess.Popen",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("mpv missing")))
    ctrl = MpvController(mpv_path="mpv.exe")
    monkeypatch.setattr(MpvController, "_wait_for_pipe",
                        lambda self, timeout: None)  # 不应被触达
    assert ctrl._launch() is False
    assert ctrl._process is None
    assert ctrl._pipe is None
    assert ctrl._running is False


def test_stop_process_waits_again_after_forced_kill():
    """进程拒绝 terminate 时，kill 后仍等待句柄退出，避免遗留子进程。"""
    proc = FakeProc(stubborn=True)
    ctrl = MpvController(mpv_path="mpv.exe")
    ctrl._process = proc
    ctrl._running = True

    ctrl.stop_process()

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert proc.wait_calls == [3, 3]
    assert ctrl._process is None
    assert ctrl._running is False


def test_stop_process_is_idempotent():
    """重复关闭不会重复操作已回收的进程。"""
    proc = FakeProc()
    ctrl = MpvController(mpv_path="mpv.exe")
    ctrl._process = proc

    ctrl.stop_process()
    ctrl.stop_process()

    assert proc.terminate_calls == 1
    assert proc.wait_calls == [3]


def test_cleanup_exceptions_do_not_block_reset(monkeypatch):
    """清理过程异常（terminate 抛 + pipe.close 抛）：不阻断 kill 与状态复位。"""
    proc = FakeProc(stubborn=True, terminate_raises=True)
    ctrl = _make_ctrl(monkeypatch, proc=proc,
                      wait_pipe_raises=TimeoutError("pipe timeout"))
    ctrl._pipe = SimpleNamespace(close=lambda: (_ for _ in ()).throw(OSError("close boom")))
    assert ctrl._launch() is False
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1, "terminate 抛异常时仍应降级 kill"
    assert ctrl._process is None
    assert ctrl._pipe is None
    assert ctrl._running is False


def test_success_path_unaffected(monkeypatch):
    """启动成功路径不受影响：进程保留、状态运行、无清理调用。"""
    proc = FakeProc()
    ctrl = _make_ctrl(monkeypatch, proc=proc)
    monkeypatch.setattr(MpvController, "_wait_for_pipe",
                        lambda self, timeout: None)
    monkeypatch.setattr(MpvController, "_init_properties",
                        lambda self, prop_timeout: None)
    assert ctrl._launch() is True
    assert ctrl._process is proc, "成功路径应保留进程对象"
    assert ctrl._running is True
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
