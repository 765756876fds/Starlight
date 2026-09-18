"""
LoL × 本地音乐 × 语音 Agent - 主入口

整合所有模块，启动系统。
"""
import os
import sys
import signal
import threading
import time
from typing import Any, Dict

# 确保项目根目录在路径中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.core.config import get_config
from app.core.event_bus import get_event_bus, Event, EventType
from app.core.command_bus import get_command_bus, CommandType
from app.core.state_store import get_state_store, StateNamespace
from app.core.scheduler import get_scheduler
from app.core.logger import get_logger
from app.core.event_log import EventLog

from app.music.controller import MusicController
from app.lol.runtime import LoLRuntime
from app.voice.runtime import VoiceRuntime
from app.speech.tts import TTSController
from app.speech.speech_judge import SpeechJudge
from app.speech.speech_queue import SpeechQueue
from app.player.history_repository import HistoryRepository
from app.player.profile_service import PlayerProfileService
from app.player.game_analysis import GameAnalysisService
from app.player.event_log_reader import EventLogReader
from app.player.query_service import PlayerQueryService
from app.player.query_handler import make_player_query_handler
from app.lol.automation.service import LoLAutomationService
from app.lol.automation.models import AutomationConfig


class Application:
    """
    主应用类

    整合所有子系统，管理生命周期。
    """

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

        # 状态
        self._running = False
        self._shutdown_event = threading.Event()

    def initialize(self):
        """初始化所有子系统"""
        print("=" * 60)
        print("  LoL × 本地音乐 × 语音 Agent")
        print("=" * 60)

        # 0. Event Log（最先启动，记录所有事件）
        print("\n[0/5] 初始化 Event Log...")
        self.event_log = EventLog(
            log_dir=self.config.get("events.log_dir", "./data/events")
        )

        # 1. 音乐模块
        print("\n[1/5] 初始化音乐模块...")
        self.music = MusicController(self.config.get_section("music"))

        # 2. LoL 模块
        print("[2/5] 初始化 LoL 模块...")
        self.lol = LoLRuntime(self.config_data)

        # 3. 语音输出
        print("[3/5] 初始化语音输出 (TTS)...")
        tts_config = self.config.get_section("tts")
        tts_config["mpv_path"] = self.config.get("music.mpv_path")
        self.tts = TTSController(tts_config)

        # 4. Speech Queue + Judge
        print("[4/5] 初始化语音播报队列 (SpeechQueue + SpeechJudge)...")
        self.speech_queue = SpeechQueue(self.tts)
        self.speech_judge = SpeechJudge(
            self.config.get_section("speech_judge"),
            queue=self.speech_queue,
        )

        # 5. 语音输入
        print("[5/5] 初始化语音输入 (ASR + Intent)...")
        self.voice = VoiceRuntime(self.config_data)

        # 注册 GameAlarm 命令处理（通过事件）
        self._register_alarm_events()

        # 注册 Player Query 命令
        self._register_player_query()

        # 注册 LoL 自动化（auto accept / auto banpick）
        self._register_automation()

        print("\n初始化完成！")

    def start(self):
        """启动所有子系统"""
        if self._running:
            return

        self._running = True
        print("\n正在启动系统...")

        # 启动 Event Log（最先启动）
        self.event_log.start()

        # 启动调度器
        self.scheduler.start()

        # 启动音乐模块
        self.music.start()

        # 扫描音乐库（后台）
        threading.Thread(target=self._scan_music_library, daemon=True).start()

        # 启动 LoL 模块
        self.lol.start()

        # 启动 TTS
        self.tts.start()

        # 启动 Speech Queue
        self.speech_queue.start()

        # 启动 Speech Judge
        self.speech_judge.start()

        # 启动语音输入
        self.voice.start()

        # 发布启动事件
        self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_STARTUP,
            data={"timestamp": time.time()},
            source="system"
        ))

        self.state_store.set(StateNamespace.SYSTEM, "running", True)
        print("\n系统已启动！按 Ctrl+C 停止。")
        print("-" * 60)

    def stop(self):
        """停止所有子系统"""
        if not self._running:
            return

        print("\n正在停止系统...")
        self._running = False

        # 发布停止事件
        self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_SHUTDOWN,
            data={},
            source="system"
        ))

        # 按顺序停止
        if self.voice:
            self.voice.stop()
        if self.speech_judge:
            self.speech_judge.stop()
        if self.speech_queue:
            self.speech_queue.shutdown()
        if self.tts:
            self.tts.stop()
        if self.lol:
            if self.automation:
                self.automation.stop()
            self.lol.stop()
        if self.music:
            self.music.stop()

        self.scheduler.stop()
        self.state_store.set(StateNamespace.SYSTEM, "running", False)

        # 最后停止 Event Log（确保所有停止事件都被记录）
        if self.event_log:
            self.event_log.stop()

        print("系统已停止。")

    def _scan_music_library(self):
        """后台扫描音乐库"""
        try:
            print("[Music] 开始扫描音乐库...")
            result = self.music.rescan()
            print(f"[Music] 扫描完成: 共 {result.get('scanned', 0)} 个文件, "
                  f"新增 {result.get('added', 0)}, 更新 {result.get('updated', 0)}")
        except Exception as e:
            print(f"[Music] 扫描失败: {e}")

    def _register_alarm_events(self):
        """注册 GameAlarm 事件处理"""
        def on_create_flash(event: Event):
            champion = event.get("champion", "")
            if champion and self.lol:
                self.lol.game_alarm.create_flash_alarm(champion)

        def on_create_spell(event: Event):
            champion = event.get("champion", "")
            spell = event.get("spell", "")
            if champion and spell and self.lol:
                # 简化：默认冷却 60 秒
                self.lol.game_alarm.create_spell_alarm(champion, spell, cooldown=60)

        def on_create_ward(event: Event):
            location = event.get("location", "当前位置")
            if self.lol:
                self.lol.game_alarm.create_ward_alarm(location)

        def on_create_custom(event: Event):
            title = event.get("title", "提醒")
            duration = event.get("duration", 120)
            if self.lol:
                self.lol.game_alarm.create_custom_alarm(title, duration)

        self.event_bus.subscribe("alarm.create_flash", on_create_flash)
        self.event_bus.subscribe("alarm.create_spell", on_create_spell)
        self.event_bus.subscribe("alarm.create_ward", on_create_ward)
        self.event_bus.subscribe("alarm.create_custom", on_create_custom)

    def _register_player_query(self):
        """注册 Player Query 命令处理器"""
        try:
            db_path = "data/player.db"
            events_dir = self.config.get(
                "events.log_dir", "./data/events"
            )
            history = HistoryRepository(db_path)
            profile = PlayerProfileService(history)
            analysis = GameAnalysisService(history, EventLogReader(events_dir))
            svc = PlayerQueryService(history, profile, analysis)
            handler = make_player_query_handler(
                svc,
                speech_queue=self.speech_queue,
                state_store=self.state_store,
            )
            self.command_bus.register(CommandType.PLAYER_QUERY, handler)
            print("[PlayerQuery] handler 已注册")
        except Exception as e:
            print(f"[PlayerQuery] 注册失败: {e}")

    def _register_automation(self):
        """注册 LoL 自动化服务与 auto accept / auto banpick 命令。"""
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
            print(f"[Automation] 已注册 enabled={cfg.enabled}")
        except Exception as e:
            print(f"[Automation] 注册失败: {e}")

    def run(self):
        """运行主循环（阻塞）"""
        self.initialize()
        self.start()

        # 注册信号处理
        def signal_handler(sig, frame):
            print("\n收到停止信号...")
            self._shutdown_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # 主循环
        try:
            while not self._shutdown_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def get_status(self) -> Dict[str, Any]:
        """获取系统状态"""
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
    """主函数"""
    app = Application()
    app.run()


if __name__ == "__main__":
    main()
