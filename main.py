"""LoL Music Agent main entry."""

import os
import sys

# Force UTF-8 stdout/stderr so Chinese prints correctly in cmd.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import signal
import threading
import time
from typing import Any, Dict

# 确保项目根目录在路径中
# frozen build: PROJECT_ROOT = exe dir（只读程序资源）；用户可写数据全部走
# USER_DATA_DIR（= %LOCALAPPDATA%\Starlight，见 app/core/path_resolve.py R1），
# 日志/事件/插件导入绝不写 Program Files。
if getattr(sys, "frozen", False):
    try:
        from app.core.path_resolve import PROJECT_ROOT, USER_DATA_DIR
    except Exception:
        PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
        USER_DATA_DIR = PROJECT_ROOT
else:
    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
    USER_DATA_DIR = PROJECT_ROOT
sys.path.insert(0, PROJECT_ROOT)

# 无黑窗兜底（PyInstaller console=False / pythonw 下 sys.stdout/stderr 可能为 None，
# print 会直接抛异常 —— 接到 devnull 保证永不崩溃）
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# 原始 stderr：绕过 TeeStream 劫持，供 LogRedirect 初始化失败时留原始痕迹
origin_stderr = sys.__stderr__
try:
    from app.core.log_redirect import install_log_redirect
    install_log_redirect(log_dir=os.path.join(USER_DATA_DIR, "logs"))
except Exception as e:
    # pythonw.exe（无控制台）下 sys.__stderr__ 可能为 None，需判空
    try:
        if origin_stderr:
            origin_stderr.write(f"[LogRedirect] install failed: {e}\n")
            origin_stderr.flush()
    except Exception:
        pass

import logging
import logging.handlers

# 标准库 logging root 配置：scanner 等 getLogger(__name__) 继承，落盘 logs/app.log。
# R4: 日志写 USER_DATA_DIR/logs（frozen 态 = %LOCALAPPDATA%\Starlight\logs，可写；
# 旧实现写 PROJECT_ROOT/logs = Program Files 只读目录，安装版启动即写失败）
try:
    os.makedirs(os.path.join(USER_DATA_DIR, "logs"), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.handlers.TimedRotatingFileHandler(
                os.path.join(USER_DATA_DIR, "logs", "app.log"),
                when="midnight",
                encoding="utf-8",
                backupCount=30,        # 只保留最近 30 天的 app.log 滚动备份
            )
        ],
    )
except Exception as e:
    # 此时 stdout 已被 TeeStream 接管，此 print 会落进 print-{date}.log 自证
    logger.error(f"[LogInit] basicConfig 失败: {e}")

from app.core.config import get_config
from app.core.event_bus import get_event_bus, Event, EventType
from app.core.command_bus import get_command_bus, CommandType
from app.core.state_store import get_state_store, StateNamespace
from app.core.scheduler import get_scheduler
from app.core.logger import get_logger
from app.core.event_log import EventLog

logger = get_logger()

from app.music.controller import MusicController
from app.lol.runtime import LoLRuntime
from app.voice.runtime import VoiceRuntime
from app.core.path_resolve import find_llama_server, music_sources_dir, seed_bundled_music_sources
from app.speech.tts import TTSController
from app.speech.speech_judge import SpeechJudge
from app.speech.speech_queue import SpeechQueue
from app.player.query_handler import make_player_query_handler
from app.lol.automation.service import LoLAutomationService
from app.lol.automation.models import AutomationConfig


class Application:
    """doc"""
    # stray

    # syntax-replaced
    """doc"""

    def __init__(self, config_path: str = None):
        # 加载配置
        self.config = get_config(config_path)
        self.config_data = self.config.data

        # 初始化核心服务
        self.event_bus = get_event_bus()
        self.command_bus = get_command_bus()
        self.state_store = get_state_store()
        self.scheduler = get_scheduler()
        self.logger = get_logger(
            log_dir=self.config.get("app.log_dir", "./logs"),
            level=self.config.get("app.log_level", "INFO"),
        )

        # 子系统
        self.music: MusicController = None
        self.lol: LoLRuntime = None
        self.voice: VoiceRuntime = None
        self.tts: TTSController = None
        self.speech_queue: SpeechQueue = None
        self.speech_judge: SpeechJudge = None
        self.event_log: EventLog = None
        self.automation = None
        self._main_window = None  # LoadingPanel 状态灯接线（_set_module_status 用）

        # 状态
        self._running = False
        self._shutdown_event = threading.Event()
        # 模块状态缓存：窗口创建前 _set_module_status 调用先落这里，
        # LoadingPanel 构造后回放。字典读写必须持锁。
        self._module_status: dict[str, str] = {}
        self._module_status_lock = threading.Lock()
        # 阶段 2：把 initialize() 里 bootstrap 的结果传给 start() → voice.start()，
        # 避免 ASRManager.initialize() 重复 ensure_and_launch 和 30s 等待。
        # "ok" / "fail" / "unknown"（未走 initialize，旧调用路径）。
        self._voice_bootstrap_result = "unknown"
        # 本进程自己拉起的 legacy llama-server（bootstrap 路径用 asr_bootstrap 内部登记）。
        # 外部复用的 ASR 不登记，退出时不杀。
        self._asr_legacy_proc = None

    _initialized = False
    def _set_module_status(self, key: str, status: str):
        """更新模块状态灯。

        1) 无论窗口是否存在，先落 _module_status 缓存（持锁）。
        2) 窗口存在时，再 emit 到 UI（不持锁，避免死锁/回调）。
        窗口未就绪时状态不丢，LoadingPanel 构造后由 replay_module_status() 回放。
        """
        with self._module_status_lock:
            self._module_status[key] = status
        try:
            w = getattr(self, "_main_window", None)
            if w is not None and hasattr(w, "_loading"):
                w._loading.status_changed.emit(key, status)
        except RuntimeError:
            # QWidget 已销毁：忽略
            pass

    def replay_module_status(self):
        """LoadingPanel 构造后，回放缓存的全部模块状态。必须在 UI 线程调用。"""
        with self._module_status_lock:
            snapshot = dict(self._module_status)
        w = getattr(self, "_main_window", None)
        if w is None or not hasattr(w, "_loading"):
            return
        for key, status in snapshot.items():
            w._loading.status_changed.emit(key, status)

    def _ensure_asr_running(self) -> bool:
        """自动拉起 ASR llama-server（如果端口 8080 未运行）。

        返回 True 表示当前代码定义的"就绪"条件成立（外部已有服务 / 本进程拉起后健康检查通过）；
        返回 False 表示模型缺失、后端全灭、legacy 超时等任何失败路径。
        不抛异常——失败由调用方按状态灯 fail 处理，不阻断其他模块。
        """
        # 优先走 asr_bootstrap（内置模型发现 + 空模型拦截 + 启动校验）
        # 开关 asr.bootstrap_enabled 默认 True；关闭时保持旧行为
        try:
            if self.config_data.get("asr", {}).get("bootstrap_enabled", True):
                from app.voice.asr_bootstrap import ensure_and_launch, _is_server_up
                ensure_and_launch(self.config_data.get("asr", {}))
                # ensure_and_launch 返回 None 既可能是"外部已有服务复用"（成功），
                # 也可能是"全部后端失败"（失败）。用 _is_server_up() 区分。
                try:
                    return bool(_is_server_up())
                except Exception:
                    return False
        except Exception as _e:
            logger.info(f"[ASR] asr_bootstrap 异常，回退内置拉起逻辑: {_e}")
        # 以下为旧逻辑（bootstrap_enabled=False 或 asr_bootstrap 异常时执行）
        import socket
        import subprocess
        import time

        asr_config = self.config_data.get("asr", {})
        server_config = asr_config.get("server", {})
        base_url = server_config.get("base_url", "http://127.0.0.1:8080/v1")
        port = 8080
        host = "127.0.0.1"

        # 检查端口是否可用
        def _port_in_use():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                result = s.connect_ex((host, port))
                s.close()
                return result == 0
            except Exception:
                return False

        if _port_in_use():
            # 端口占用 ≠ 就绪：可能是其他服务，也可能是 llama-server 刚监听未加载模型。
            # 与 bootstrap 同一标准：/v1/models 返回 200 且 data 非空。
            try:
                import requests as _rq2
                resp = _rq2.get(f"http://{host}:{port}/v1/models", timeout=1.5)
                if resp.status_code == 200 and resp.json().get("data"):
                    logger.info(f"[ASR] 端口 {port} 已有可用 ASR 服务，直接连接")
                    return True
                logger.info(f"[ASR] 端口 {port} 被占用但非可用 ASR（data 空），继续尝试拉起")
            except Exception as e:
                logger.info(f"[ASR] 端口 {port} 被占用但健康检查失败: {e}")
            return False

        # 需要启动 ASR
        model_path = asr_config.get("qwen3_asr_model", "")
        mmproj_path = asr_config.get("qwen3_asr_mmproj", "")
        if not model_path:
            logger.warning("[ASR] WARNING: 模型路径为空且内置模型未发现，跳过拉起（避免 router 模式）")
            return False

        # llama-server 路径：配置 → 自动搜索（PATH / LM Studio 扩展目录）→ 降级
        llama_server = find_llama_server(asr_config)
        if not llama_server:
            logger.warning("[ASR] WARNING: llama-server 未找到（设置 asr.llama_server，或安装 LM Studio 扩展）")
            logger.info("[ASR] 请手动启动 ASR，或检查 llama-server 路径")
            return False

        logger.info(f"[ASR] 端口 {port} 未运行，正在自动拉起 llama-server...")
        logger.info(f"[ASR] model: {model_path}")
        logger.info(f"[ASR] mmproj: {mmproj_path}")

        args = [
            llama_server,
            "--model", model_path,
            "--mmproj", mmproj_path,
            "--port", str(port),
            "--host", host,
            "-t", "4",
            "--no-warmup",
        ]

        try:
            # 日志写 USER_DATA_DIR/logs，不依赖当前工作目录（避免 CWD 污染/Program Files 只读）
            _asr_log_dir = os.path.join(USER_DATA_DIR, "logs")
            try:
                os.makedirs(_asr_log_dir, exist_ok=True)
            except Exception:
                _asr_log_dir = os.devnull
            _out_path = os.path.join(_asr_log_dir, "asr_server_out.txt") if _asr_log_dir != os.devnull else os.devnull
            _err_path = os.path.join(_asr_log_dir, "asr_server_err.txt") if _asr_log_dir != os.devnull else os.devnull
            # 文件句柄由 Popen 接管（子进程退出后关闭）；Popen 失败时需手动关闭
            _out_fp = open(_out_path, "w")
            _err_fp = open(_err_path, "w")
            try:
                proc = subprocess.Popen(
                    args,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=_out_fp,
                    stderr=_err_fp,
                )
            except Exception:
                _out_fp.close()
                _err_fp.close()
                raise
            # 登记为本进程所有的 legacy llama-server，退出时 terminate。
            # 外部复用的 ASR（_port_in_use 分支）不走这里，不会被误杀。
            self._asr_legacy_proc = proc
            import atexit as _atexit
            def _kill_legacy_asr(p=proc):
                try:
                    if p.poll() is None:
                        p.terminate()
                        try:
                            p.wait(timeout=3)
                        except Exception:
                            p.kill()
                except Exception:
                    pass
            _atexit.register(_kill_legacy_asr)
            logger.info("[ASR] llama-server 已启动，等待就绪...")
        except Exception as e:
            logger.error(f"[ASR] 启动 llama-server 失败: {e}")
            self.logger.error(f"[ASR] 启动 llama-server 失败: {e}")
            return False

        # 等待 ASR 就绪（最多 60 秒）：轮询 /models 返回 200 且 data 非空才算就绪。
        # 与 asr_bootstrap._is_server_up 同一标准——避免 router 模式（-m ""）下
        # HTTP 200 但 data 为空被误判为就绪。
        import requests as _rq
        for i in range(60):
            time.sleep(1)
            try:
                resp = _rq.get(f"http://{host}:{port}/v1/models", timeout=1)
                if resp.status_code == 200:
                    payload = resp.json()
                    if payload.get("data"):
                        logger.info(f"[ASR] 就绪（等待 {i+1} 秒）")
                        return True
            except Exception:
                pass
            if i % 10 == 0:
                logger.info(f"[ASR] 等待中... ({i}s)")

        logger.info("[ASR] 拉起超时（60秒），模型可能未就绪，继续启动")
        return False

    def initialize(self):
        # 类级单例守卫：整个进程内只 initialize 一次。
        # 入口即置 True，而非函数末尾——当前生产代码只有 run() 一个调用点（main.py:1016），
        # 无递归或并发初始化路径（已 grep 确认）。
        # 契约：initialize() 中途异常后，_initialized 保持 True，不支持同一进程内重试；
        # _handle_startup_failure() 已尽力 stop() 清理已构造模块，但不重置本标志。
        # 若未来需要重试，必须先证明 stop() 能完整回收所有子进程/句柄，再引入状态机。
        if Application._initialized:
            logger.info("[Startup] already initialized, skip")
            return
        Application._initialized = True
        """初始化所有子系统"""
        logger.info("=" * 60)
        logger.info("  LoL × 本地音乐 × 语音 Agent")
        logger.info("=" * 60)

        # 0. Event Log（最先启动，记录所有事件）
        logger.info("\n[0/5] 初始化 Event Log...")
        self.event_log = EventLog(
            log_dir=self.config.get("events.log_dir", os.path.join(USER_DATA_DIR, "data", "events"))
        )

        # 1. 音乐模块
        logger.info("\n[1/5] 初始化音乐模块...")
        self.music = MusicController(self.config.get_section("music"))
        # 注册 online music source
        try:
            from app.music.online.manager import OnlineSourceManager
            from app.music.online.sources.demo_source import DemoSource
            from app.music.online.sources.archive_source import ArchiveSource
            self.online_music_manager = OnlineSourceManager()
            # demo/archive 为演示/归档源：默认禁用（网络慢且不稳定，会拖慢每次在线搜索与
            # 取 URL）。保留注册能力，需要时在 config.json 的 online_music 段设置
            # demo_source=true / archive_source=true 重新启用。
            _om_cfg = (self.config_data.get("online_music") or {})
            if _om_cfg.get("demo_source", False):
                self.online_music_manager.register(DemoSource())
                logger.info("[Music] demo 音源已启用（online_music.demo_source=true）")
            if _om_cfg.get("archive_source", False):
                self.online_music_manager.register(ArchiveSource())
                logger.info("[Music] archive 音源已启用（online_music.archive_source=true）")
            # R4: 插件目录走 music_sources_dir()（frozen 态 = USER_DATA_DIR/data/music_sources，
            # 可写；安装版 Program Files 只读，导入写不进 exe 目录）。
            # 首启把随包内置插件种到用户目录（幂等，不覆盖用户导入的插件）。
            custom_dir = str(music_sources_dir())
            try:
                seed_bundled_music_sources()
            except Exception as _e:
                logger.info(f"[Music] 内置插件种子失败: {_e}")
            try:
                self.online_music_manager.load_custom_sources(custom_dir)
            except Exception as e:
                logger.info(f"[Music] 自定义源加载失败: {e}")
            try:
                from app.music.online.sources.js_source import JSMusicSource
                _ms_root = music_sources_dir()
                _mf = str(_ms_root / "musicfree")
                _lx = str(_ms_root / "lx")
                self._js_source = JSMusicSource(_mf, _lx, user_variables=(self.config_data.get("online_music", {}).get("js_bridge", {}) or {}).get("user_variables") or {})
                self.online_music_manager.register(self._js_source)
                self._set_module_status("js", "ok" if self._js_source.available else "fail")
            except Exception as e:
                logger.info(f"[Music] JS bridge 初始化失败: {e}")
                self._set_module_status("js", "fail")
            self.music.set_online_manager(self.online_music_manager)
            # 同步「音源管理」UI 导入的自定义音源（config/music_sources.json）
            try:
                self.music.sync_custom_sources()
            except Exception as e:
                logger.error(f"[Music] 自定义音源同步失败: {e}")
            logger.info(f"[Music] online source 已注册 {self.online_music_manager.list_sources()}")
        except Exception as e:
            logger.info(f"[Music] online source 初始化失败: {e}")

        # 2. LoL 模块
        logger.info("[2/5] 初始化 LoL 模块...")
        self.lol = LoLRuntime(self.config_data)

        # 3. 语音输出
        logger.info("[3/5] 初始化语音输出(TTS)...")
        tts_config = self.config.get_section("tts")
        tts_config["mpv_path"] = self.config.get("music.mpv_path")
        self.tts = TTSController(tts_config)

        # 4. Speech Queue + Judge
        logger.info("[4/5] 初始化语音播放队列(SpeechQueue + SpeechJudge)...")
        self.speech_queue = SpeechQueue(self.tts)
        self.speech_judge = SpeechJudge(
            self.config.get_section("speech_judge"),
            queue=self.speech_queue,
        )

        # 敌方成装语音提醒（equipment_notifier，初始化失败不阻塞主流程）
        self._equip_enabled = True
        try:
            from app.lol.equipment_notifier import EquipmentUpdateNotifier
            self.equip_notifier = EquipmentUpdateNotifier(self.speech_queue)
            # 装备提醒持久化：启动读 ui_prefs.json（默认开）
            from app.ui.settings_store import load_ui_prefs
            self._equip_enabled = bool(load_ui_prefs().get("equip_notify", True))
        except Exception as _e:
            logger.error(f"[Equipment] notifier 初始化失败: {_e}")
            self.equip_notifier = None

        # 5. 语音输入
        logger.info("[5/5] 初始化语音输入(ASR + Intent)...")
        self._set_module_status("asr", "loading")
        asr_enabled_raw = self.config_data.get("asr", {}).get("enabled", True)
        # 兼容字符串 "false"/"true"（某些配置导入路径可能给字符串）
        if isinstance(asr_enabled_raw, str):
            asr_enabled = asr_enabled_raw.strip().lower() not in ("false", "0", "no", "off", "")
        else:
            asr_enabled = bool(asr_enabled_raw)
        if not asr_enabled:
            logger.info("[ASR] asr.enabled=false，跳过 ASR 启动/下载/健康检查")
            self._voice_bootstrap_result = "disabled"
            asr_ok = False
        else:
            asr_ok = self._ensure_asr_running()
            self._voice_bootstrap_result = "ok" if asr_ok else "fail"
        self.voice = VoiceRuntime(self.config_data, tts_provider=lambda: self.tts)
        self.voice.speech_queue_provider = lambda: self.speech_queue
        # V2-2：惰性 mpv/控制器 provider（全局暂停/继续实时 IPC 确认用）
        self.voice.music_mpv_provider = lambda: getattr(self.music, "mpv", None) if self.music else None
        self.voice.music_controller_provider = lambda: self.music if self.music else None
        def _movie_ctrl():
            try:
                mt = getattr(self, "_movie_panel", None)
                if mt is not None:
                    return getattr(mt, "_movie", None)
            except Exception:
                pass
            return None
        self.voice.movie_controller_provider = _movie_ctrl
        def _movie_mpv():
            try:
                mv = _movie_ctrl()
                if mv is not None:
                    return getattr(mv, "mpv", None)
            except Exception:
                pass
            return None
        self.voice.movie_mpv_provider = _movie_mpv
        self._set_module_status("asr", "disabled" if not asr_enabled else ("ok" if asr_ok else "fail"))
        self._set_module_status("intent", "ok")

        # 注册 GameAlarm 指令处理（通过事件）
        self._register_alarm_events()

        # 注册 Player Query 指令
        self._register_player_query()

        # 注册 LoL 自动化（auto accept / auto banpick）
        self._register_automation()

        # 注册手动技能计时器（cooldown）
        self._register_cooldown()

        # 注册 OngoingGameService（ChampSelect 加载 10 玩家）

        logger.info("\n初始化完成！")

    def start(self):
        """doc"""
        if self._running:
            return

        self._running = True
        logger.info("\n现在启动系统...")

        # 启动 Event Log（最先启动）
        self.event_log.start()
        self._set_module_status("eventlog", "ok")

        # 启动调度器
        self.scheduler.start()

        # 启动音乐模块
        try:
            _music_ok = self.music.start()
        except Exception as e:
            logger.info(f"[Music] module start failed: {e!r}")
            _music_ok = False
        self._set_module_status("music", "ok" if _music_ok else "fail")

        # 扫描音乐库（后台）
        threading.Thread(target=self._scan_music_library, daemon=True).start()

        # 启动 LoL 模块
        self.lol.start()
        self._set_module_status("lol", "ok")

        # OngoingGameService 构造必须在 lcu discovery 之后：
        # 否则 _build_direct_getter / acct._direct 独立直连失效，退化为共享 lcu.get，
        # 游戏内共享连接断连窗口会导致 summoner/ranked/history 全部失败。
        self._register_ongoing()


        # 启动 OngoingGameService（ChampSelect 加载 10 玩家）
        if hasattr(self, 'ongoing_service') and self.ongoing_service:
            self.ongoing_service.start()
            logger.info("[Ongoing] 加载服务已启动")

        # 注册一键连招 ComboManager（依赖 ongoing_service 识别当前英雄）
        self._register_combo()
        # 启动 TTS
        self.tts.start()
        self._set_module_status("tts", "ok")

        # 启动 Speech Queue
        self.speech_queue.start()
        self._set_module_status("speech", "ok")

        # 启动 Speech Judge
        self.speech_judge.start()

        # 启动语音输入
        self.voice.start(bootstrap_result=self._voice_bootstrap_result)

        # 发布启动事件
        self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_STARTUP,
            data={"timestamp": time.time()},
            source="system"
        ))

        self.state_store.set(StateNamespace.SYSTEM, "running", True)
        logger.info("\nSystem started! Press Ctrl+C to stop.")
        logger.info("-" * 60)

    def stop(self):
        """doc"""
        if not self._running:
            return

        logger.info("\n现在停止系统...")
        self._running = False

        # 每一步独立 try/except：某个模块清理失败不能中断后续清理链。
        # 顺序保持原有顺序不变；scheduler.stop() 和 event_log.stop() 必须有机会执行。
        def _safe(label, fn):
            try:
                fn()
            except Exception:
                logger.error(f"[Shutdown] {label} 清理失败", exc_info=True)

        # 发布停止事件（失败不影响后续清理）
        _safe("event_bus.publish(SYSTEM_SHUTDOWN)", lambda: self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_SHUTDOWN,
            data={},
            source="system"
        )))

        # 按顺序停止各模块
        if self.voice:
            _safe("voice.stop", self.voice.stop)
        if self.speech_judge:
            _safe("speech_judge.stop", self.speech_judge.stop)
        if self.speech_queue:
            _safe("speech_queue.shutdown", self.speech_queue.shutdown)
        if self.tts:
            _safe("tts.stop", self.tts.stop)
        if self.lol:
            if self.automation:
                _safe("automation.stop", self.automation.stop)
            _safe("lol.stop", self.lol.stop)
        if self.music:
            _safe("music.stop", self.music.stop)

        # 批次1：主动回收本进程拉起的 legacy llama-server（不依赖 atexit）。
        # 外部复用的 ASR 不登记，这里不会杀到它。
        proc = getattr(self, "_asr_legacy_proc", None)
        if proc is not None:
            self._asr_legacy_proc = None  # 幂等：atexit 兜底再调时 poll()=None 已结束
            def _kill(p=proc):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except Exception:
                        p.kill()
                        try:
                            p.wait(timeout=3)
                        except Exception:
                            pass
            _safe("legacy_asr.terminate", _kill)

        # 批次N-2：主动回收 bootstrap 路径拉起的 llama-server（与 legacy 并列）。
        # 复用 asr_bootstrap 的锁与进程存活判断，不复制终止逻辑；幂等，
        # 重复 stop() 或后续 atexit 兜底均安全。单步失败不阻断后续清理。
        def _shutdown_bootstrap_asr():
            from app.voice import asr_bootstrap
            asr_bootstrap.shutdown_server()
        _safe("bootstrap_asr.shutdown", _shutdown_bootstrap_asr)

        if getattr(self, "combo_manager", None):
            _safe("combo_manager.stop", self.combo_manager.stop)

        _safe("scheduler.stop", self.scheduler.stop)
        _safe("state_store.set(running=False)",
              lambda: self.state_store.set(StateNamespace.SYSTEM, "running", False))

        # 最后停止 Event Log（确保所有停止事件都被记录）
        if self.event_log:
            _safe("event_log.stop", self.event_log.stop)

        logger.info("...")

        # 收尾：flush+close 重定向落盘句柄（print-{date}.log / error-{date}.log）
        try:
            from app.core.log_redirect import close_redirect
            close_redirect()
        except Exception:
            pass

    def _scan_music_library(self):
        """doc"""
        try:
            logger.info("...")
            result = self.music.rescan()
            logger.info(f"[Music] 扫描完成: 共 {result.get('total', 0)} 首, "
                  f"新增 {result.get('added', 0)}, 更新 {result.get('updated', 0)}")
        except Exception as e:
            logger.info(f"[Music] 扫描失败: {e}")

    def _register_alarm_events(self):
        """注册 GameAlarm 事件处理"""
        def _say(text, priority="high"):
            # 快速语音反馈：直接走 SPEECH_SAY -> TTS，绕过普通游戏播报队列
            try:
                self.event_bus.publish(Event(
                    event_type=EventType.SPEECH_SAY,
                    data={"text": text, "priority": priority},
                    source="alarm_feedback",
                ))
            except Exception:
                self.logger.error("[Alarm] SPEECH_SAY 事件发布失败", exc_info=True)

        def on_create_flash(event):
            logger.info(f"[P0CD DEBUG] on_create_flash called: event_type={event.event_type}, data={event.data}")
            champion = event.get("champion", "")
            if not (champion and self.lol):
                logger.info(f"[P0CD DEBUG] early return: champion={champion}, lol={self.lol}")
                _say("没确认到目标")
                return
            # T0：报告瞬间读当前 2999 快照，算 base + SSH，冻结 effective CD。
            eff = 300.0
            snap = None
            ssh = 0.0
            ssh_src = []
            base = 300.0
            raw_champ = champion
            from app.lol.champion.normalizer import normalize_champion_name
            canonical = normalize_champion_name(champion) or champion
            enemy = None
            try:
                from app.lol.data.summoner_spell_catalog import SummonerSpellCatalog
                from app.lol.cooldown.calculator import CooldownCalculator
                from app.lol.cooldown.ssh_provider import summoner_spell_haste
                snap = self.lol.current_snapshot
                if canonical and snap:
                    enemy = snap.get_player_by_champion(canonical)
                sp = SummonerSpellCatalog().get_by_name("Flash")
                base = float(sp.cooldown) if sp else 300.0
                if enemy is not None:
                    ssh, ssh_src, _st = summoner_spell_haste(enemy.items, enemy.runes)
                eff = CooldownCalculator.for_summoner(base, summoner_spell_haste=ssh) or base
            except Exception:
                eff = base
            logger.info(f"[P0CD] raw={raw_champ} normalized={canonical} enemy_found={enemy is not None} "
                  f"base={base} ssh={ssh} src={ssh_src} eff={eff} "
                  f"gt={snap.game_time if snap else None}")
            disp = canonical or champion
            src = {
                "champion": disp, "raw": raw_champ, "spell": "flash",
                "game_time": snap.game_time if snap else None,
                "base_cd": base, "ssh": ssh, "ssh_sources": ssh_src,
                "effective_cd": eff,
            }
            self.lol.game_alarm.create_flash_alarm(disp, cooldown=eff, source_snapshot=src)
            _say(f"记好了，{disp}闪现")

        def on_create_spell(event):
            champion = event.get("champion", "")
            spell = event.get("spell", "")
            if champion and spell and self.lol:
                self.lol.game_alarm.create_spell_alarm(champion, spell, cooldown=60)
                _say(f"记好了，{champion} {spell}")
            else:
                _say("没确认到目标")

        def on_create_ward(event):
            location = event.get("location", "当前位置")
            if self.lol:
                self.lol.game_alarm.create_ward_alarm(location)
                _say("记好眼位了")

        def on_create_custom(event):
            title = event.get("title", "")
            duration = event.get("duration", 0)
            content = event.get("content", "")
            if self.lol and duration and duration > 0:
                self.lol.game_alarm.create_custom_alarm(
                    title or "提醒", duration, lead_time=0.0, content=content)
                m, s = divmod(int(duration), 60)
                t = (f"{m}分" if m else "") + f"{s}秒"
                _say(f"记好了，{t}后提醒你{content}" if content else "记好了，到点提醒你")
            else:
                _say("没有确认提醒时间")

        def on_alarm_list(event):
            if not self.lol:
                _say("当前没有记录"); return
            active = self.lol.game_alarm.get_user_memories()
            if not active:
                _say("当前没有记录"); return
            names = "、".join(a.title for a in active[:5])
            _say(f"记了 {len(active)} 个：{names}")

        def on_alarm_remaining(event):
            if not self.lol:
                _say("当前没有记录"); return
            active = self.lol.game_alarm.get_user_memories()
            if not active:
                _say("当前没有记录"); return
            a = sorted(active, key=lambda x: x.game_time_end)[0]
            gt = self.lol.game_alarm._current_game_time
            secs = max(0, int(a.remaining(gt)))
            m, s = divmod(secs, 60)
            _say(f"{a.title}，还有 {m} 分 {s} 秒")

        def on_alarm_cancel_last(event):
            if not self.lol:
                _say("当前没有记录"); return
            a = self.lol.game_alarm.cancel_last_user_memory()
            if a is None:
                _say("当前没有记录"); return
            _say(f"已取消：{a.title}")

        self.event_bus.subscribe("alarm.create_flash", on_create_flash)
        self.event_bus.subscribe("alarm.create_spell", on_create_spell)
        self.event_bus.subscribe("alarm.create_ward", on_create_ward)
        self.event_bus.subscribe("alarm.create_custom", on_create_custom)
        self.event_bus.subscribe("alarm.list", on_alarm_list)
        self.event_bus.subscribe("alarm.remaining", on_alarm_remaining)
        self.event_bus.subscribe("alarm.cancel_last", on_alarm_cancel_last)

    def _register_player_query(self):
        """doc"""
        try:
            db_path = os.path.join(PROJECT_ROOT, "data", "player.db")
            events_dir = self.config.get(
                "events.log_dir", os.path.join(PROJECT_ROOT, "data", "events")
            )
            def _snapshot_provider():
                try:
                    return self.lol.current_snapshot if self.lol else None
                except Exception:
                    return None
            handler = make_player_query_handler(
                None,
                speech_queue=self.speech_queue,
                state_store=self.state_store,
                snapshot_provider=_snapshot_provider,
            )
            self.command_bus.register(CommandType.PLAYER_QUERY, handler)
            logger.info("...")
        except Exception as e:
            logger.info(f"[PlayerQuery] 注册失败: {e}")

    def _register_automation(self):
        """doc"""
        try:
            cfg = AutomationConfig.from_dict(self.config.get("automation", {}))
            self.automation = LoLAutomationService(
                lcu=self.lol.lcu,
                config=cfg,
            )

            def on_accept_toggle(command):
                enabled = self.automation.toggle_auto_accept()
                return {"auto_accept": enabled}

            def on_banpick_toggle(command):
                enabled = self.automation.toggle_auto_banpick()
                return {"auto_banpick": enabled}

            self.command_bus.register(CommandType.AUTO_ACCEPT_TOGGLE, on_accept_toggle)
            self.command_bus.register(CommandType.AUTO_BANPICK_TOGGLE, on_banpick_toggle)
            if cfg.enabled:
                self.automation.start()
                self._set_module_status("automation", "ok")
            else:
                self._set_module_status("automation", "disabled")
            logger.info(f"[Automation] 已注册 enabled={cfg.enabled}")
        except Exception as e:
            logger.info(f"[Automation] 注册失败: {e}")

    def _register_combo(self):
        """注册一键连招 ComboManager（一期：配置加载+键盘监听+执行引擎+只读UI）。

        默认关闭，须在自动化面板手动开启。依赖 ongoing_service 识别我方英雄。
        """
        try:
            from app.combo.manager import ComboManager
            self.combo_manager = ComboManager(
                ongoing_service=getattr(self, "ongoing_service", None),
            )
            # 前台门持久化：启动读 ui_prefs.json（默认开）
            try:
                from app.ui.settings_store import load_ui_prefs
                self.combo_manager.set_window_gate(
                    bool(load_ui_prefs().get("combo_window_gate", True)))
            except Exception:
                pass
            self.combo_manager.start()
            logger.info("[Combo] ComboManager 已启动（默认关闭，需在自动化面板开启）")
        except Exception as e:
            logger.error(f"[Combo] 注册失败: {e}")
            self.logger.error(f"[Combo] 注册失败: {e}")

    def _register_cooldown(self):
        """doc"""
        try:
            from app.lol.cooldown.manager import CooldownManager as _CM
            from app.lol.cooldown.events import (
                COOLDOWN_REMIND,
            )

            # SpeechQueue 閫傞厤锛氭妸 reminder 鏂囨湰濉炶繘 SpeechQueue锛堣嫢宸插垵濮嬪寲锛?
            def _speech_enqueue(text: str, event_id: str):
                try:
                    sq = getattr(self, "speech_queue", None) or getattr(self, "tts_queue", None)
                    if sq is None:
                        return
                    # 閫傞厤 SpeechQueue.enqueue(SpeechItem)
                    from app.speech.speech_queue import SpeechItem, PRIORITY_A
                    import time as _t
                    item = SpeechItem(text=text, priority=PRIORITY_A, expire_at=_t.time()+30.0, event_id=event_id)
                    sq.enqueue(item)
                except Exception:
                    self.logger.error(f"[Cooldown] 语音入队失败: {text[:50]!r}", exc_info=True)

            def _on_event(code: str, data: dict):
                # 绠€鍗曟棩蹇楋紱TTS 鐢?_speech_enqueue 璐熻矗
                logger.info(f"[Cooldown] {code}: {data.get('text') or data}")

            def _haste_provider(champion, side="enemy"):
                # 返回 (ability_haste, summoner_spell_haste, champion_level, runes)
                try:
                    rt = getattr(self, "lol", None)
                    snap = rt.current_snapshot if rt else None
                    if not snap or not champion:
                        return (0.0, 0.0, 0, ())
                    from app.lol.enemy.roster import EnemyRoster
                    from app.lol.data.item_haste import sum_ability_haste
                    from app.lol.cooldown.ssh_provider import summoner_spell_haste
                    prof = EnemyRoster().find(snap, champion)
                    if not prof:
                        return (0.0, 0.0, 0, ())
                    ah, _src = sum_ability_haste(prof.items)
                    ssh, _sources, _status = summoner_spell_haste(prof.items, getattr(prof, 'runes', []))
                    return (ah, ssh, int(prof.level or 0), tuple(prof.runes or ()))
                except Exception:
                    return (0.0, 0.0, 0, ())

            self.cooldown_manager = _CM(
                on_event=_on_event,
                speech_enqueue=_speech_enqueue,
                haste_provider=_haste_provider,
            )
            self.command_bus.register(CommandType.COOLDOWN_START,
                                     self.cooldown_manager.handle_start)
            self.command_bus.register(CommandType.COOLDOWN_CANCEL,
                                     self.cooldown_manager.handle_cancel)
            self.command_bus.register(CommandType.COOLDOWN_CANCEL_ALL,
                                     self.cooldown_manager.handle_cancel_all)
            self.command_bus.register(CommandType.COOLDOWN_LIST,
                                     self.cooldown_manager.handle_list)
            self.command_bus.register(CommandType.COOLDOWN_QUERY,
                                     self.cooldown_manager.handle_query)
            # 冷却计时锚定游戏内时钟（暂停时不前进）
            self.event_bus.subscribe("game.time_update",
                                     self.cooldown_manager.on_game_time_update)
            self.event_bus.subscribe("game.ended",
                                     lambda e: self.cooldown_manager.reset_clock())
            # GAME_ENDED 时清空所有 reminder（Phase 3-K.6）
            self.event_bus.subscribe("game.ended",
                                     lambda e: self.cooldown_manager.clear())
            
            # Phase 3-K.7.2: Cooldown 动态修正事件接入
            def _on_cooldown_adjustment(event):
                """处理 cooldown.adjustment 事件。
                
                data 字段:
                    reminder_id: str
                    adjustment_seconds: float
                    event_id: str
                """
                try:
                    reminder_id = event.get("reminder_id", "")
                    adjustment = float(event.get("adjustment_seconds", 0))
                    event_id = event.get("event_id", "")
                    if reminder_id and adjustment > 0 and event_id:
                        self.cooldown_manager.apply_adjustment(
                            reminder_id, adjustment, event_id
                        )
                except Exception as e:
                    logger.error(f"[Cooldown] adjustment event error: {e}")
            
            self.event_bus.subscribe("cooldown.adjustment", _on_cooldown_adjustment)
            logger.info("[Cooldown] adjustment event handler registered")
            
            # Phase 3-K.7.4.4: AxiomArcEvaluator 生产接入
            try:
                from app.lol.cooldown.axiom_arc import AxiomArcEvaluator
                def _axiom_snap_provider():
                    return self.lol.current_snapshot if self.lol else None
                self.axiom_evaluator = AxiomArcEvaluator(
                    cooldown_manager=self.cooldown_manager,
                    event_bus=self.event_bus,
                    get_snapshot=_axiom_snap_provider,
                    champion_catalog=getattr(self.cooldown_manager, '_champions', None),
                )
                self.axiom_evaluator.start()
                logger.info("[AxiomArc] evaluator registered")
            except Exception as _e:
                logger.error(f"[AxiomArc] evaluator 注册失败: {_e}")
            logger.info("...")
            # A5 经济状态边沿提醒（仅跨边界时一句短话，低噪音）
            try:
                from app.player.economy_advisor import make_economy_advisor
                def _snap_provider():
                    return self.lol.current_snapshot if self.lol else None
                adv = make_economy_advisor(_snap_provider, self.speech_queue, enabled=True)
                self.event_bus.subscribe("game.time_update", adv["on_time_update"])
                self.event_bus.subscribe("game.ended", adv["on_game_reset"])
                logger.info("[A5] economy advisor registered")
            except Exception as _e:
                logger.error(f"[A5] economy advisor 注册失败: {_e}")

            # A6 敌方成装语音提醒（equipment_notifier，game.time_update 每帧 ~1Hz）
            try:
                if getattr(self, "equip_notifier", None):
                    def _equip_snap_provider():
                        rt = getattr(self, "lol", None)
                        snap = rt.current_snapshot if rt else None
                        return snap

                    def _on_equip_tick(event):
                        try:
                            if not getattr(self, "_equip_enabled", True):
                                return
                            snap = _equip_snap_provider()
                            if snap and getattr(snap, "players", None):
                                self.equip_notifier.update(snap.players)
                        except Exception as _e:
                            logger.info(f"[Equipment] 更新异常: {_e}")

                    def _on_equip_game_end(event):
                        try:
                            self.equip_notifier.clear()
                        except Exception as _e:
                            logger.info(f"[Equipment] 清理异常: {_e}")

                    self.event_bus.subscribe("game.time_update", _on_equip_tick)
                    self.event_bus.subscribe("game.ended", _on_equip_game_end)
                    logger.info("[Equipment] notifier registered")
            except Exception as _e:
                logger.error(f"[Equipment] notifier 注册失败: {_e}")
        except Exception as e:
            logger.info(f"[Cooldown] 注册失败: {e}")

    def _register_ongoing(self):
        try:
            from app.lol.ongoing.service import OngoingGameService
            from app.lol.ongoing.account_history import AccountMatchHistoryService
            from app.lol.ongoing.gsm_provider import SgpGsmProvider
            lcu_get = self.lol.lcu.get if self.lol and self.lol.lcu else None
            acct = AccountMatchHistoryService(lcu_get, lcu_conn=(self.lol.lcu if self.lol else None)) if lcu_get else None
            gsm = SgpGsmProvider(lcu_get, lcu_conn=(self.lol.lcu if self.lol else None)) if lcu_get else None
            def _snap_for_ongoing():
                try:
                    lol = getattr(self, "lol", None)
                    return getattr(lol, "current_snapshot", None)
                except Exception:
                    return None
            _ONGOING_DEDUP_CODES = (
                "ongoing.session_source", "ongoing.gsm_supplement", "ongoing.live_merged",
                "ongoing.session_created", "history.error", "room.player", "history_summary",
                "ongoing.conn_skip",   # LCU 未连接时每秒轮询打一次，去重
            )
            _ongoing_last: dict = {}

            def _ongoing_on_event(code: str, data: dict):
                if code in _ONGOING_DEDUP_CODES:
                    cur = str(data)
                    if _ongoing_last.get(code) == cur:
                        return  # 与上次相同，跳过（高频日志去重）
                    _ongoing_last[code] = cur
                logger.info(f"[Ongoing] {code}: {data}")

            logger.info("[Ongoing] 高频日志去重已启用（session_source/gsm_supplement/live_merged 相同内容不重复打印）")
            self.ongoing_service = OngoingGameService(
                lcu=self.lol.lcu if self.lol else None,
                on_event=_ongoing_on_event,
                account_history_service=acct,
                recent_n=20,
                snapshot_provider=_snap_for_ongoing,
                gsm_provider=gsm,
                deobfuscate_enabled=self.config.get("ongoing.deobfuscate_enabled", True),
            )
        except Exception as e:
            logger.info(f"[Ongoing] 注册失败: {e}")
            self.logger.error(f"[Ongoing] 注册失败: {e}")

    def _handle_startup_failure(self, exc: BaseException) -> None:
        """initialize()/start() 在 QApplication 创建前同步执行失败时的兜底。

        - 完整 traceback 写日志（不弹到用户界面）。
        - 尽力回收已创建的子系统（stop() 只在 _running=True 时才跑，
          而 initialize 中途失败时 _running 还是 False，这里强制置位后再调）。
        - 清理本身再抛异常不能覆盖原始启动异常。
        - 非 headless 模式弹 QMessageBox；headless 或 GUI 也起不来就退到 stderr。
        """
        import traceback as _tb
        err_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        try:
            logger.error(f"[Startup] initialize/start 失败:\n{err_text}")
        except Exception:
            # 日志系统本身可能没初始化好
            print("[Startup] initialize/start 失败:", file=sys.stderr)
            print(err_text, file=sys.stderr)

        # 尽力清理。stop() 入口检查 _running，初始化中途失败时为 False，
        # 这里临时置位让它走完整 teardown；清理解包在 try 里，不覆盖原异常。
        try:
            self._running = True
            self.stop()
        except Exception:
            try:
                logger.error("[Startup] 失败清理过程再次抛异常:", exc_info=True)
            except Exception:
                pass

        # 用户可见提示。headless 不弹窗；GUI 起不来就退到 stderr。
        is_headless = "--headless" in sys.argv
        if not is_headless:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication(sys.argv)
                summary = f"{type(exc).__name__}: {exc}"
                log_dir = self.config.get("app.log_dir", "./logs")
                QMessageBox.critical(
                    None,
                    "Starlight 启动失败",
                    f"{summary}\n\n详细错误已写入日志。\n日志目录: {log_dir}\n\n可关闭此窗口退出。",
                )
            except Exception:
                try:
                    logger.error("[Startup] QMessageBox 创建失败，降级到 stderr", exc_info=True)
                except Exception:
                    pass
                print(f"[Startup] 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            print(f"[Startup] headless 模式失败: {type(exc).__name__}: {exc}", file=sys.stderr)

    def run(self) -> int:
        """返回进程退出码：0 正常，1 启动失败。"""
        def _do_startup():
            self.initialize()
            self.start()

        def _on_startup_failure(exc):
            try:
                self._handle_startup_failure(exc)
            except Exception:
                try:
                    logger.error("[Startup] 错误处理过程本身也失败", exc_info=True)
                except Exception:
                    pass

        def signal_handler(sig, frame):
            logger.info("\n收到停止信号...")
            self._shutdown_event.set()
            try:
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
                if app is not None:
                    app.quit()
            except Exception:
                self.logger.error("[Shutdown] Qt app.quit() 失败", exc_info=True)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # headless 模式：无 GUI，同步初始化
        if "--headless" in sys.argv:
            try:
                _do_startup()
            except Exception as e:
                _on_startup_failure(e)
                return 1
            try:
                while not self._shutdown_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
            return 0

        # UI 模式：主线程跑 Qt（QApplication 必须在主线程创建）
        try:
            from PySide6.QtWidgets import QApplication
            from app.ui.main_window import MainWindow
            from app.ui.tray import TrayIcon
        except Exception as e:
            logger.info(f"[UI] PySide6 未安装，使用 headless: {e}")
            try:
                _do_startup()
            except Exception as e2:
                _on_startup_failure(e2)
                return 1
            try:
                while not self._shutdown_event.is_set():
                    time.sleep(0.5)
            finally:
                self.stop()
            return 0

        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        try:
            from app.ui.theme.applier import apply_dark_theme
            apply_dark_theme(app)
        except Exception as _e:
            logger.info(f'[UI] theme load failed: {_e}')

        logger.info("[Run] creating MainWindow")
        window = MainWindow(
            automation_service=None,
            cooldown_manager=None,
            music=None,
        )
        self._main_window = window
        window.show()
        window.raise_()
        window.activateWindow()
        logger.info(f"[Run] MainWindow shown at {window.geometry()}")
        # 回放窗口创建前缓存的模块状态
        self.replay_module_status()

        state = {"done": False, "error": None, "switched": False}

        def backend():
            try:
                logger.info("[Run] background: initialize")
                _do_startup()
                logger.info("[Run] background: done")
                state["done"] = True
            except Exception as e:
                import traceback
                traceback.print_exc()
                state["error"] = f"{type(e).__name__}: {e}"

        import threading
        threading.Thread(target=backend, daemon=True, name="StartupBackend").start()

        from PySide6.QtCore import QTimer

        def check():
            if state["error"] and not state["switched"]:
                state["switched"] = True
                logger.error(f"[Run] backend error: {state['error']}")
                try:
                    window._loading.show_failure(state["error"])
                except Exception:
                    pass
                # 不切页，停留在 LoadingPanel 显示错误。
                # P0 修复：立即回收已构造的子系统。stop() 入口检查 _running，
                # 而 initialize 失败时 _running=False（start() 未跑），这里强制置位再 stop，
                # 与 _handle_startup_failure 的做法一致；不弹 QMessageBox（错误已在面板上）。
                try:
                    self._running = True
                    self.stop()
                except Exception:
                    logger.error("[Run] 启动失败后清理出错", exc_info=True)
                return
            if state["done"] and not state["switched"]:
                state["switched"] = True
                logger.info("[Run] switching to tabs")
                try:
                    self._switch_to_tabs(window)
                    logger.info("[Run] switched")
                    self._start_overlay()
                    timer.stop()
                except Exception:
                    import traceback
                    traceback.print_exc()

        timer = QTimer()
        timer.timeout.connect(check)
        timer.start(500)
        self._startup_timer = timer

        logger.info("[Run] entering app.exec()")
        rc = 0
        try:
            rc = app.exec()
        finally:
            self.stop()
        return rc

    def _start_overlay(self):
        try:
            from app.ui.overlay.overlay_model import OverlayModel
            from app.ui.overlay.overlay_window import OverlayWindow
            cm = getattr(self, "cooldown_manager", None)
            def phase():
                try:
                    # 读 runtime 缓存的 gameflow phase（_poll_lcu 每 1s 更新，后台线程写入）。
                    # 原实现在 UI 线程发同步 LCU 请求，对局结束/LCU 重启窗口 read timeout=5s
                    # 冻结 UI 线程 → Windows 标"（未响应）"。改读缓存后 UI 线程零 IO。
                    lol = getattr(self, "lol", None)
                    if lol and hasattr(lol, "_last_known_phase"):
                        # 语义不变：overlay 只认 InProgress；断连时缓存保留旧值（守卫依赖，不清）
                        return "InProgress" if lol._last_known_phase == "InProgress" else "None"
                    return "None"
                except Exception:
                    return "None"
            def snapshot():
                try:
                    return getattr(getattr(self, "lol", None), "current_snapshot", None)
                except Exception:
                    return None
            m = OverlayModel(
                snapshot_provider=snapshot,
                phase_provider=phase,
                cooldown_manager_provider=lambda: cm,
            )
            self._overlay_window = OverlayWindow(m)
            # 接线：主窗口"显示游戏内 Overlay"勾选框 → set_overlay_refs（勾=显示、取消=隐藏）
            if getattr(self, "_main_window", None) is not None:
                self._main_window.set_overlay_refs(m, self._overlay_window)
            
            # H.4.4: 连接 Overlay 点击信号 → EventBus → alarm.create_flash
            def on_spell_clicked(champion_name: str, spell_name: str):
                logger.info(f"[Overlay] spell clicked: champion={champion_name}, spell={spell_name}")
                # 规范化 spell 名称：把 2999 的变体名称转换成 canonical name
                # 例如："治疗术" → "治疗"，"解封的传送" → "传送"，"引燃" → "点燃"
                from app.ui.overlay.overlay_model import _normalize_spell_name
                from app.lol.data.summoner_spell_catalog import SummonerSpellCatalog
                _spell_catalog = SummonerSpellCatalog()
                canonical_spell = _normalize_spell_name(spell_name, _spell_catalog)
                logger.info(f"[Overlay] normalized spell: {spell_name} → {canonical_spell}")
                # H.4.4: 通过 CommandBus → CooldownManager（统一语音和 Overlay 入口）
                from app.core.command_bus import Command, CommandType
                self.command_bus.execute(Command(
                    command_type=CommandType.COOLDOWN_START,
                    params={
                        "champion": champion_name,
                        "skill": canonical_spell,
                        "side": "enemy",
                    },
                    source="overlay_click",
                ))
                # 语音提示：确认记录成功
                from app.core.event_bus import EventType
                self.event_bus.publish(Event(
                    event_type=EventType.SPEECH_SAY,
                    data={"text": f"记好了，{champion_name}{canonical_spell}", "priority": "high"},
                    source="overlay_click",
                ))
            
            self._overlay_window.spell_clicked.connect(on_spell_clicked)
            logger.info("[Run] overlay started")
        except Exception:
            import traceback; traceback.print_exc()
    def _switch_to_tabs(self, window):
        logger.info("[Run] _switch_to_tabs start")
        from app.ui.widgets.main_tabs import MainTabs
        logger.info("[Run] building MainTabs")
        tabs = MainTabs(
            automation_service=getattr(self, "automation", None),
            cooldown_manager=getattr(self, "cooldown_manager", None),
            music=getattr(self, "music", None),
            ongoing_service=getattr(self, "ongoing_service", None),
            snapshot_provider=lambda: (self.lol.current_snapshot if getattr(self, "lol", None) else None),
            combo_manager=getattr(self, "combo_manager", None),
            services_dict={"lcu": getattr(self.lol, "lcu", None) if getattr(self, "lol", None) else None},
            tts=getattr(self, "tts", None),
            speech_queue=getattr(self, "speech_queue", None),
            equip_toggle=lambda on: setattr(self, "_equip_enabled", bool(on)),
            overlay_toggle=window._on_overlay_toggle,
            overlay_enabled=True,
                    )
        logger.info("[Run] MainTabs built, switching")
        # 统一运行时状态：MainWindow 底部唯一 StatusBar 读取这套真实服务
        try:
            window._services = {
                "lcu": getattr(getattr(self, "automation", None), "_lcu", None),
                "asr": getattr(getattr(self, "voice", None), "asr", None),
                "intent": getattr(getattr(self, "voice", None), "intent_router", None),
                "voice": getattr(self, "voice", None),
            }
        except Exception as e:
            logger.info(f"[UI] services wiring failed: {e}")
        window.switch_to_tabs(tabs)
        self._movie_panel = tabs._panels.get("movie")
        # R001: 注入电影暂停确认回调到音乐控制器
        try:
            if self._movie_panel is not None and hasattr(self, "music"):
                movie_ctrl = getattr(self._movie_panel, "_movie", None)
                if movie_ctrl is not None and hasattr(movie_ctrl, "pause_and_confirm"):
                    self.music.set_movie_pauser(movie_ctrl.pause_and_confirm)
                    logger.info("[R001] movie pauser injected to music controller")
        except Exception as e:
            logger.warning(f"[R001] movie pauser wiring failed: {e}")
        logger.info("[Run] switched, showing tray")
        from app.ui.tray import TrayIcon
        tray = TrayIcon(window, on_quit=self.stop)
        tray.show()
        self._tray = tray
        self._main_window = window
        logger.info("[Run] tray shown")


    def get_status(self) -> Dict[str, Any]:
        """doc"""
        return {
            "running": self._running,
            "music": {
                "playing": self.music.is_playing() if self.music else False,
                "current_song": self.music.get_current_song() if self.music else None,
                "volume": self.music.get_volume() if self.music else 0,
                "song_count": self.music.get_song_count() if self.music else 0,
            },
            "lol": {
                "game_active": self.lol.is_game_active if self.lol else False,
                "lcu_connected": self.lol.lcu.connected if self.lol else False,
            },
            "voice": {
                "listening": self.voice.is_listening() if self.voice else False,
            },
        }


def main():
    """doc"""
    app = Application()
    sys.exit(app.run())


if __name__ == "__main__":
    main()





