"""启动失败保护测试（main.Application._handle_startup_failure）。

不实例化真实 Application（避免拉起 event_bus/scheduler/DB），
用 unittest.mock 构造最小对象调用被测方法。
所有测试强制 headless，避免在 CI/无显示环境创建真实 QApplication。
"""
import sys
from unittest.mock import patch, MagicMock


def _make_app():
    """构造一个最小 Application 替身，只挂被测方法需要的属性。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = False
    app._shutdown_event = __import__("threading").Event()
    app.config = MagicMock()
    app.config.get.return_value = "./logs"
    return app


def test_handle_startup_failure_logs_and_calls_stop(capsys):
    """initialize() 抛异常：完整 traceback 写日志/stderr，stop() 被调用。"""
    app = _make_app()
    called = {"stop": False}
    def fake_stop():
        called["stop"] = True
        app._running = False
    app.stop = fake_stop

    try:
        raise RuntimeError("boom-startup")
    except RuntimeError as e:
        with patch.object(sys, "argv", ["main.py", "--headless"]):
            app._handle_startup_failure(e)

    assert called["stop"] is True
    out = capsys.readouterr().err + capsys.readouterr().out
    assert "boom-startup" in out
    assert "RuntimeError" in out


def _make_partial_app(stage: str):
    """构造一个模拟 initialize/start 中途失败的 Application。

    stage:
      - "early": initialize 刚设 _initialized=True，还没建任何模块（event_log 都没有）
      - "mid":   music/lol/tts/speech_queue/speech_judge 已建，voice 还没建
      - "started": 所有模块都建了，start() 跑了一半
    """
    from main import Application
    app = Application.__new__(Application)
    app._running = False
    app._shutdown_event = __import__("threading").Event()
    app.config = MagicMock()
    app.config.get.return_value = "./logs"
    # 模拟 __init__ 里建的核心服务
    app.event_bus = MagicMock()
    app.scheduler = MagicMock()
    app.state_store = MagicMock()
    app.event_log = MagicMock()
    # __init__ 里 None 的子系统
    app.music = None
    app.lol = None
    app.voice = None
    app.tts = None
    app.speech_queue = None
    app.speech_judge = None
    app.automation = None
    app.combo_manager = None
    app.online_music_manager = None
    if stage in ("mid", "started"):
        app.music = MagicMock()
        app.lol = MagicMock()
        app.tts = MagicMock()
        app.speech_queue = MagicMock()
        app.speech_judge = MagicMock()
    if stage == "started":
        app.voice = MagicMock()
        app._running = True  # start() 已设
    return app


def test_partial_init_early_failure_cleanup_does_not_crash(capsys):
    """initialize 极早期失败（只有 event_bus/scheduler/state_store/event_log），
    stop() 不应因缺失属性崩。"""
    app = _make_partial_app("early")
    try:
        raise RuntimeError("config-load-failed")
    except RuntimeError as e:
        with patch.object(sys, "argv", ["main.py", "--headless"]):
            app._handle_startup_failure(e)  # 不应抛
    # 关键：event_bus.publish / scheduler.stop 仍被调到
    app.event_bus.publish.assert_called()
    app.scheduler.stop.assert_called()


def test_partial_init_mid_failure_stops_created_modules(capsys):
    """initialize 中途失败（music/lol/tts/speech 已建，voice 未建）。
    已建模块的 stop 都应被调到；voice=None 不应炸。"""
    app = _make_partial_app("mid")
    try:
        raise RuntimeError("asr-bootstrap-failed")
    except RuntimeError as e:
        with patch.object(sys, "argv", ["main.py", "--headless"]):
            app._handle_startup_failure(e)
    app.music.stop.assert_called()
    app.lol.stop.assert_called()
    app.tts.stop.assert_called()
    app.speech_queue.shutdown.assert_called()
    app.speech_judge.stop.assert_called()
    # voice 是 None，不应调
    assert app.voice is None


def test_start_mid_failure_cleanup_independent(capsys):
    """start() 中途失败（所有模块已建，_running=True）。

    发现：stop() 本身不是 per-module try/except，tts.stop 抛异常会中断清理链，
    music/lol/voice 的 stop 不会被调到。这是 stop() 既有的健壮性问题，
    不是本轮 _handle_startup_failure 引入的。这里只验证：
      1) _handle_startup_failure 不会让异常逃逸；
      2) 原始启动异常仍被记录；
      3) _running 被 stop() 复位。
    """
    app = _make_partial_app("started")
    app.tts.stop.side_effect = RuntimeError("tts-teardown-fail")
    try:
        raise RuntimeError("voice-start-failed")
    except RuntimeError as e:
        with patch.object(sys, "argv", ["main.py", "--headless"]):
            app._handle_startup_failure(e)  # 不应抛
    # _running 应被 stop() 复位（stop 第一行就 _running=False）
    assert app._running is False
    out = capsys.readouterr().err + capsys.readouterr().out
    assert "voice-start-failed" in out


# ===== stop() 容错测试（任务 B）=====

def _make_running_app():
    """构造一个 _running=True 的完整 app 替身，模拟正常启动后的 stop() 调用。"""
    app = _make_partial_app("started")  # music/lol/tts/speech/voice/scheduler/event_log/event_bus 全 mock
    return app


def test_stop_tts_failure_continues_to_others():
    """tts.stop() 抛异常，后续 lol/music/scheduler/event_log 仍被调用。"""
    app = _make_running_app()
    app.tts.stop.side_effect = RuntimeError("tts-fail")
    app.stop()
    # 顺序：voice → speech_judge → speech_queue → tts(炸) → lol → music → scheduler → event_log
    app.voice.stop.assert_called()
    app.lol.stop.assert_called()
    app.music.stop.assert_called()
    app.scheduler.stop.assert_called()
    app.event_log.stop.assert_called()


def test_stop_voice_failure_continues():
    """voice.stop() 抛异常，后续模块仍清理。"""
    app = _make_running_app()
    app.voice.stop.side_effect = RuntimeError("voice-fail")
    app.stop()
    app.speech_judge.stop.assert_called()
    app.tts.stop.assert_called()
    app.lol.stop.assert_called()
    app.scheduler.stop.assert_called()


def test_stop_event_bus_publish_failure_continues():
    """event_bus.publish(SYSTEM_SHUTDOWN) 抛异常，后续清理继续。"""
    app = _make_running_app()
    app.event_bus.publish.side_effect = RuntimeError("bus-fail")
    app.stop()
    app.voice.stop.assert_called()
    app.music.stop.assert_called()
    app.scheduler.stop.assert_called()


def test_stop_scheduler_failure_still_stops_event_log():
    """scheduler.stop() 抛异常，event_log.stop() 仍执行。"""
    app = _make_running_app()
    app.scheduler.stop.side_effect = RuntimeError("sched-fail")
    app.stop()
    app.event_log.stop.assert_called()


def test_stop_multiple_failures_dont_override():
    """多个模块同时抛异常：都被记录，不互相覆盖，不崩溃。"""
    app = _make_running_app()
    app.voice.stop.side_effect = RuntimeError("v1")
    app.tts.stop.side_effect = RuntimeError("t1")
    app.scheduler.stop.side_effect = RuntimeError("s1")
    app.stop()  # 不应抛
    app.event_log.stop.assert_called()


def test_stop_is_idempotent():
    """stop() 连续调两次：第二次因 _running=False 直接返回，不重复清理。"""
    app = _make_running_app()
    app.stop()
    app.voice.stop.reset_mock()
    app.stop()
    app.voice.stop.assert_not_called()


# ===== 退出码测试（任务 A）=====

def test_run_returns_1_on_startup_failure(monkeypatch):
    """initialize/start 抛异常 → run() 返回 1。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = False
    app._shutdown_event = __import__("threading").Event()
    app.config = MagicMock()
    app.config.get.return_value = "./logs"
    def boom():
        raise RuntimeError("init-fail")
    app.initialize = boom
    app.start = MagicMock()
    app._handle_startup_failure = MagicMock()
    monkeypatch.setattr(sys, "argv", ["main.py", "--headless"])
    rc = app.run()
    assert rc == 1
    app._handle_startup_failure.assert_called_once()


def test_handle_startup_failure_internal_error_still_returns_1(monkeypatch):
    """_handle_startup_failure 内部辅助步骤再抛异常，run() 仍返回 1。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = False
    app._shutdown_event = __import__("threading").Event()
    app.config = MagicMock()
    app.config.get.return_value = "./logs"
    def boom_handle(exc):
        raise RuntimeError("handle-broken")
    app.initialize = MagicMock(side_effect=RuntimeError("init-boom"))
    app.start = MagicMock()
    app._handle_startup_failure = boom_handle
    monkeypatch.setattr(sys, "argv", ["main.py", "--headless"])
    rc = app.run()
    assert rc == 1


def test_run_headless_returns_0(monkeypatch):
    """headless 正常退出返回 0。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = False
    app._shutdown_event = __import__("threading").Event()
    app.config = MagicMock()
    app.config.get.return_value = "./logs"
    app.initialize = MagicMock()
    app.start = MagicMock()
    app.stop = MagicMock()
    # 让 _shutdown_event 立刻 set，避免 sleep
    app._shutdown_event.set()
    monkeypatch.setattr(sys, "argv", ["main.py", "--headless"])
    rc = app.run()
    assert rc == 0


def test_handle_startup_failure_stop_raises_does_not_override(capsys):
    """清理过程再抛异常：原始启动异常仍在输出里，不二次崩溃。"""
    app = _make_app()
    def bad_stop():
        raise RuntimeError("cleanup-failed")
    app.stop = bad_stop

    try:
        raise ValueError("original-error")
    except ValueError as e:
        with patch.object(sys, "argv", ["main.py", "--headless"]):
            app._handle_startup_failure(e)  # 不应抛

    out = capsys.readouterr().err + capsys.readouterr().out
    assert "original-error" in out
    # 清理异常走 logger.error 写文件，不一定到 stderr；这里只验证原异常未被覆盖、未二次崩溃


def test_handle_startup_failure_headless_no_dialog():
    """--headless：不创建 QApplication/QMessageBox。"""
    app = _make_app()
    app.stop = MagicMock()

    try:
        raise OSError("no-mpv")
    except OSError as e:
        with patch.object(sys, "argv", ["main.py", "--headless"]), \
             patch("PySide6.QtWidgets.QMessageBox") as mb, \
             patch("PySide6.QtWidgets.QApplication") as QA:
            app._handle_startup_failure(e)

    mb.critical.assert_not_called()
    QA.assert_not_called()


def test_handle_startup_failure_gui_path_attempts_dialog():
    """非 headless：尝试走 QMessageBox 路径（用 mock，不真起 Qt）。"""
    app = _make_app()
    app.stop = MagicMock()

    try:
        raise KeyError("cfg-key")
    except KeyError as e:
        with patch.object(sys, "argv", ["main.py"]), \
             patch("PySide6.QtWidgets.QApplication") as QA, \
             patch("PySide6.QtWidgets.QMessageBox") as mb:
            QA.instance.return_value = None
            app._handle_startup_failure(e)
            QA.assert_called()
            mb.critical.assert_called_once()
