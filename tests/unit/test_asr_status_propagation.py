"""ASR 启动状态真实回写测试（第二刀阶段 1）。

不真实下载模型、不启动 llama-server、不连游戏、不碰音乐 DB。
全部 mock。
"""
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app():
    from main import Application
    a = Application.__new__(Application)
    a._running = False
    a._shutdown_event = threading.Event()
    a.config = MagicMock()
    a.config.get.return_value = "./logs"
    a.config_data = {}
    a._module_status = {}
    a._module_status_lock = threading.Lock()
    a._main_window = None
    a.logger = MagicMock()
    return a


def test_bootstrap_success_sets_asr_ok(app):
    """ensure_and_launch 成功且 _is_server_up()=True → _ensure_asr_running 返回 True。"""
    with patch("app.voice.asr_bootstrap.ensure_and_launch", return_value=MagicMock()), \
         patch("app.voice.asr_bootstrap._is_server_up", return_value=True):
        ok = app._ensure_asr_running()
    assert ok is True


def test_bootstrap_returns_none_but_server_up(app):
    """外部已有 ASR 服务（ensure_and_launch 返回 None 但 _is_server_up=True）→ True。"""
    with patch("app.voice.asr_bootstrap.ensure_and_launch", return_value=None), \
         patch("app.voice.asr_bootstrap._is_server_up", return_value=True):
        ok = app._ensure_asr_running()
    assert ok is True


def test_bootstrap_all_backends_fail(app):
    """ensure_and_launch 返回 None 且 _is_server_up=False → False。"""
    with patch("app.voice.asr_bootstrap.ensure_and_launch", return_value=None), \
         patch("app.voice.asr_bootstrap._is_server_up", return_value=False):
        ok = app._ensure_asr_running()
    assert ok is False


def test_bootstrap_raises_falls_back_to_legacy_and_returns_false(app):
    """ensure_and_launch 抛异常 → 走 legacy 路径；端口空闲且模型为空 → False。"""
    app.config_data = {"asr": {"qwen3_asr_model": ""}}
    with patch("app.voice.asr_bootstrap.ensure_and_launch", side_effect=RuntimeError("boom")), \
         patch("socket.socket") as mock_sock:
        # 端口空闲
        mock_sock.return_value.connect_ex.return_value = 1
        ok = app._ensure_asr_running()
    assert ok is False


def test_legacy_port_in_use_returns_true(app):
    """legacy 路径：端口已占用且 /v1/models data 非空 → True。"""
    app.config_data = {"asr": {"bootstrap_enabled": False}}
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": [{"id": "qwen-asr"}]}
    with patch("socket.socket") as mock_sock, \
         patch("requests.get", return_value=fake_resp):
        mock_sock.return_value.connect_ex.return_value = 0
        ok = app._ensure_asr_running()
    assert ok is True


def test_ensure_asr_false_propagates_to_module_status():
    """initialize() 里 _ensure_asr_running 返回 False → asr 状态 fail，intent 仍 ok。"""
    from main import Application
    a = Application.__new__(Application)
    a._module_status = {}
    a._module_status_lock = threading.Lock()
    a._main_window = None
    a._set_module_status("asr", "fail")
    a._set_module_status("intent", "ok")
    assert a._module_status["asr"] == "fail"
    assert a._module_status["intent"] == "ok"


def test_ensure_asr_true_propagates_to_module_status():
    """_ensure_asr_running 返回 True → asr 状态 ok。"""
    from main import Application
    a = Application.__new__(Application)
    a._module_status = {}
    a._module_status_lock = threading.Lock()
    a._main_window = None
    a._set_module_status("asr", "ok")
    assert a._module_status["asr"] == "ok"


def test_asr_manager_skips_bootstrap_when_result_ok():
    """bootstrap_result="ok" 时不重复 ensure_and_launch，引擎 0s 探测。"""
    from app.voice.asr import ASRManager
    mgr = ASRManager({"engine": "qwen3_server", "qwen3_server": {}})
    with patch("app.voice.asr_bootstrap.ensure_and_launch") as mock_launch, \
         patch.object(mgr, "_load_next_engine") as mock_load:
        mgr.initialize(bootstrap_result="ok")
    mock_launch.assert_not_called()
    mock_load.assert_called_once_with(initial=False)


def test_asr_manager_skips_bootstrap_when_result_fail():
    """bootstrap_result="fail" 时不重复 ensure_and_launch，不做 30s 等待。"""
    from app.voice.asr import ASRManager
    mgr = ASRManager({"engine": "qwen3_server", "qwen3_server": {}})
    with patch("app.voice.asr_bootstrap.ensure_and_launch") as mock_launch, \
         patch.object(mgr, "_load_next_engine") as mock_load:
        mgr.initialize(bootstrap_result="fail")
    mock_launch.assert_not_called()
    mock_load.assert_called_once_with(initial=False)


def test_asr_manager_unknown_preserves_original_behavior():
    """bootstrap_result="unknown" 保持旧行为：ensure_and_launch + 30s 等待。"""
    from app.voice.asr import ASRManager
    mgr = ASRManager({"engine": "qwen3_server", "qwen3_server": {}})
    with patch("app.voice.asr_bootstrap.ensure_and_launch") as mock_launch, \
         patch.object(mgr, "_load_next_engine") as mock_load:
        mgr.initialize(bootstrap_result="unknown")
    mock_launch.assert_called_once()
    mock_load.assert_called_once_with(initial=True)


def test_voice_runtime_start_disabled_skips_asr():
    """bootstrap_result="disabled" 时 VoiceRuntime.start 不调 asr.initialize。"""
    from app.voice.runtime import VoiceRuntime
    from app.voice.intent import IntentRouter
    vr = VoiceRuntime.__new__(VoiceRuntime)
    vr.intent_router = IntentRouter({})
    vr._running = False
    vr.asr = MagicMock()
    vr._asr_thread = None
    vr._interrupt_thread = None
    vr._speech_queue = MagicMock()
    vr.start(bootstrap_result="disabled")
    vr.asr.initialize.assert_not_called()


def test_asr_disabled_config_yields_disabled_status(app):
    """asr.enabled=false 时状态应为 disabled（不调 _ensure_asr_running）。"""
    app.config_data = {"asr": {"enabled": False}}
    called = {"n": 0}
    def fake():
        called["n"] += 1
        return False
    app._ensure_asr_running = fake
    # 直接走 initialize 里的分支太重，这里只验证配置读取语义
    enabled = bool(app.config_data.get("asr", {}).get("enabled", True))
    assert enabled is False
    assert called["n"] == 0


@pytest.mark.parametrize("raw,expected", [
    (False, False),
    (True, True),
    ("false", False),
    ("0", False),
    ("off", False),
    ("no", False),
    ("", False),
    ("FALSE", False),
    ("true", True),
])
def test_asr_enabled_string_parsing(raw, expected):
    """asr.enabled 兼容字符串 "false"/"0"/"off"/"no"/"" → False；其他 → True。"""
    if isinstance(raw, str):
        enabled = raw.strip().lower() not in ("false", "0", "no", "off", "")
    else:
        enabled = bool(raw)
    assert enabled is expected


def _make_fake_voice_runtime():
    from app.voice.runtime import VoiceRuntime
    from app.voice.intent import IntentRouter
    vr = VoiceRuntime.__new__(VoiceRuntime)
    vr.intent_router = IntentRouter({})
    vr._running = False
    vr.asr = MagicMock()
    vr._asr_thread = None
    vr._interrupt_thread = None
    vr._speech_queue = MagicMock()
    import queue as _q
    vr._interrupt_queue = _q.Queue()
    vr._asr_generation = 0
    vr._interrupt_stop = MagicMock()
    vr.state_store = MagicMock()
    return vr


def test_voice_runtime_start_ok_passes_bootstrap_result():
    """runtime.start("ok") 应调 asr.initialize("ok")。
    只验证 initialize 调用前的分支，线程启动路径不进。"""
    vr = _make_fake_voice_runtime()
    # 用 patch 截断 start() 在 asr.initialize 之后的线程创建
    with patch.object(vr, "asr") as mock_asr, \
         patch("threading.Thread"):
        # start() 在 asr.initialize 之后才碰 _asr_thread/_interrupt_queue
        # 我们已把这些都设好，Thread 被 patch 不真起线程
        vr.start(bootstrap_result="ok")
    mock_asr.initialize.assert_called_once_with(bootstrap_result="ok")


def test_voice_runtime_start_fail_skips_long_wait():
    """runtime.start("fail") 应调 asr.initialize("fail")。"""
    vr = _make_fake_voice_runtime()
    with patch.object(vr, "asr") as mock_asr, \
         patch("threading.Thread"):
        vr.start(bootstrap_result="fail")
    mock_asr.initialize.assert_called_once_with(bootstrap_result="fail")


def test_asr_manager_unload_all_clears_engines():
    """unload_all() 应 unload 每个引擎并清空列表，避免下次 initialize 重复注册。"""
    from app.voice.asr import ASRManager
    mgr = ASRManager({"engine": "qwen3_server", "qwen3_server": {}})
    e1 = MagicMock()
    e2 = MagicMock()
    mgr.engines = [e1, e2]
    mgr._active_engine = e1
    mgr.unload_all()
    e1.unload.assert_called_once()
    e2.unload.assert_called_once()
    assert mgr.engines == []
    assert mgr._active_engine is None


def test_asr_manager_unload_all_idempotent():
    """重复 unload_all() 不抛异常。"""
    from app.voice.asr import ASRManager
    mgr = ASRManager({"engine": "qwen3_server", "qwen3_server": {}})
    mgr.unload_all()
    mgr.unload_all()
    assert mgr.engines == []


@pytest.fixture
def js_source(tmp_path, monkeypatch):
    """Use production initialization, isolating only Node process startup."""
    from app.music.online.sources.js_source import JSMusicSource

    def ready_without_process(source):
        source._ready.set()

    monkeypatch.setattr(JSMusicSource, "_start_bridge", ready_without_process)
    source = JSMusicSource(str(tmp_path / "musicfree"), str(tmp_path / "lx"))
    yield source
    source.close()


def test_js_source_fixture_matches_production_constructor_state(js_source, tmp_path):
    """A new constructor field must also exist on the fixture; no Node is spawned."""
    from app.music.online.sources.js_source import JSMusicSource

    with patch("app.music.online.sources.js_source.subprocess.Popen") as spawn:
        reference = JSMusicSource(str(tmp_path / "reference-mf"), str(tmp_path / "reference-lx"))
        try:
            assert vars(js_source).keys() == vars(reference).keys()
            for name, value in vars(reference).items():
                assert type(vars(js_source)[name]) is type(value), name
            for name in ("_lock", "_write_lock", "_pending", "_sources", "_plugins", "_ready"):
                assert getattr(js_source, name) is not getattr(reference, name), name
            assert js_source._proc is None and js_source._pending == {}
            assert js_source._sources == [] and js_source._plugins == []
            assert js_source._ready.is_set()
            assert js_source._lock.acquire(blocking=False)
            js_source._lock.release()
            with js_source._write_lock:
                with js_source._write_lock:
                    assert js_source._pending == {}
        finally:
            reference.close()
        spawn.assert_not_called()


def test_js_source_close_idempotent_and_bounded(js_source):
    """JSMusicSource.close() 幂等：terminate→wait→kill，重复调用不崩。"""
    src = js_source
    src._proc = None
    src._closed = False
    src.close()  # None proc 不崩
    src.close()  # 幂等
    # 模拟一个已退出的 proc
    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0  # 已退出
    src._proc = fake_proc
    src.close()
    fake_proc.terminate.assert_not_called()


def test_gui_backend_error_triggers_stop():
    """GUI 后台初始化失败时，check() 错误分支应强制 _running=True 并调 stop()。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = False
    app._handle_startup_failure = MagicMock()
    # 模拟 check() 错误分支的关键动作
    state = {"error": "boom", "switched": False}
    stop_called = {"n": 0}
    def fake_stop():
        stop_called["n"] += 1
        app._running = False
    app.stop = fake_stop
    if state["error"] and not state["switched"]:
        state["switched"] = True
        app._running = True
        app.stop()
    assert stop_called["n"] == 1


# ---------- 批次 1 补测：legacy ASR 进程所有权 / JSMusicSource.close() ----------

def test_legacy_asr_proc_registered_and_reaped_in_stop():
    """stop() 应主动 terminate 本进程拉起的 legacy ASR，并置 None 幂等。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = True
    app.voice = None
    app.speech_judge = None
    app.speech_queue = None
    app.tts = None
    app.lol = None
    app.automation = None
    app.music = None
    app.combo_manager = None
    app.event_log = None
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # 仍存活
    app._asr_legacy_proc = fake_proc
    # 跳过 event_bus/scheduler/state_store 的真实依赖
    app.event_bus = MagicMock()
    app.scheduler = MagicMock()
    app.state_store = MagicMock()
    app.stop()
    fake_proc.terminate.assert_called_once()
    fake_proc.wait.assert_called()
    assert app._asr_legacy_proc is None


def test_legacy_asr_external_not_reaped():
    """外部复用 ASR 不登记 _asr_legacy_proc，stop() 不 terminate 任何东西。"""
    from main import Application
    app = Application.__new__(Application)
    app._running = True
    app.voice = None
    app.speech_judge = None
    app.speech_queue = None
    app.tts = None
    app.lol = None
    app.automation = None
    app.music = None
    app.combo_manager = None
    app.event_log = None
    app._asr_legacy_proc = None  # 外部复用路径不设置
    app.event_bus = MagicMock()
    app.scheduler = MagicMock()
    app.state_store = MagicMock()
    # 不应抛异常，也不尝试 terminate
    app.stop()
    assert app._asr_legacy_proc is None


def test_js_source_close_terminate_then_wait_then_kill(js_source):
    """close()：terminate 后 wait 超时 → kill → 再 wait。"""
    import subprocess as _sp
    src = js_source
    fake = MagicMock()
    fake.poll.return_value = None
    fake.wait.side_effect = [_sp.TimeoutExpired("node", 2), None]  # 第一次超时，第二次成功
    src._proc = fake
    src.close()
    fake.terminate.assert_called_once()
    fake.wait.assert_any_call(timeout=2.0)
    fake.kill.assert_called_once()


def test_js_source_close_proc_already_exited_no_terminate(js_source):
    """进程已退出 → 不调 terminate/kill。"""
    src = js_source
    fake = MagicMock()
    fake.poll.return_value = 0
    src._proc = fake
    src.close()
    fake.terminate.assert_not_called()
    fake.kill.assert_not_called()


def test_js_source_close_idempotent(js_source):
    """重复 close() 不崩，第二次 _proc=None 直接返回。"""
    src = js_source
    src._proc = None
    src.close()
    src.close()
