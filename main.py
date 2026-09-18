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

# 纭繚椤圭洰鏍圭洰褰曞湪璺緞涓?
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

try:
    from app.core.log_redirect import install_log_redirect
    install_log_redirect()
except Exception:
    pass

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
    """doc"""
    # stray

    # syntax-replaced
    """doc"""

    def __init__(self, config_path: str = None):
        # 鍔犺浇閰嶇疆
        self.config = get_config(config_path)
        self.config_data = self.config.data

        # 鍒濆鍖栨牳蹇冩湇鍔?
        self.event_bus = get_event_bus()
        self.command_bus = get_command_bus()
        self.state_store = get_state_store()
        self.scheduler = get_scheduler()
        self.logger = get_logger(
            log_dir=self.config.get("app.log_dir", "./logs"),
            level=self.config.get("app.log_level", "INFO"),
        )

        # 瀛愮郴缁?
        self.music: MusicController = None
        self.lol: LoLRuntime = None
        self.voice: VoiceRuntime = None
        self.tts: TTSController = None
        self.speech_queue: SpeechQueue = None
        self.speech_judge: SpeechJudge = None
        self.event_log: EventLog = None
        self.automation = None

        # 鐘舵€?
        self._running = False
        self._shutdown_event = threading.Event()

    _initialized = False
    def initialize(self):
        if Application._initialized:
            print("[Startup] already initialized, skip")
            return
        Application._initialized = True
        """鍒濆鍖栨墍鏈夊瓙绯荤粺"""
        print("=" * 60)
        print("  LoL 脳 鏈湴闊充箰 脳 璇煶 Agent")
        print("=" * 60)

        # 0. Event Log锛堟渶鍏堝惎鍔紝璁板綍鎵€鏈変簨浠讹級
        print("\n[0/5] 鍒濆鍖?Event Log...")
        self.event_log = EventLog(
            log_dir=self.config.get("events.log_dir", "./data/events")
        )

        # 1. 闊充箰妯″潡
        print("\n[1/5] 鍒濆鍖栭煶涔愭ā鍧?..")
        self.music = MusicController(self.config.get_section("music"))
        # 娉ㄥ唽 online music source
        try:
            from app.music.online.manager import OnlineSourceManager
            from app.music.online.sources.demo_source import DemoSource
            from app.music.online.sources.archive_source import ArchiveSource
            self.online_music_manager = OnlineSourceManager()
            self.online_music_manager.register(DemoSource())
            self.online_music_manager.register(ArchiveSource())
            import os
            custom_dir = os.path.join(".", "data", "music_sources")
            try:
                self.online_music_manager.load_custom_sources(custom_dir)
            except Exception as e:
                print(f"[Music] 鑷畾涔夋簮鍔犺浇澶辫触: {e}")
            try:
                from pathlib import Path as _P
                from app.music.online.sources.js_source import JSMusicSource
                _mf = str(_P("data/music_sources/musicfree").absolute())
                _lx = str(_P("data/music_sources/lx").absolute())
                self._js_source = JSMusicSource(_mf, _lx, user_variables=(self.config_data.get("online_music", {}).get("js_bridge", {}) or {}).get("user_variables") or {})
                self.online_music_manager.register(self._js_source)
            except Exception as e:
                print(f"[Music] JS bridge 鍒濆鍖栧け璐? {e}")
            self.music.set_online_manager(self.online_music_manager)
            print(f"[Music] online source 宸叉敞鍐? {self.online_music_manager.list_sources()}")
        except Exception as e:
            print(f"[Music] online source 鍒濆鍖栧け璐? {e}")

        # 2. LoL 妯″潡
        print("[2/5] 鍒濆鍖?LoL 妯″潡...")
        self.lol = LoLRuntime(self.config_data)

        # 3. 璇煶杈撳嚭
        print("[3/5] 鍒濆鍖栬闊宠緭鍑?(TTS)...")
        tts_config = self.config.get_section("tts")
        tts_config["mpv_path"] = self.config.get("music.mpv_path")
        self.tts = TTSController(tts_config)

        # 4. Speech Queue + Judge
        print("[4/5] 鍒濆鍖栬闊虫挱鎶ラ槦鍒?(SpeechQueue + SpeechJudge)...")
        self.speech_queue = SpeechQueue(self.tts)
        self.speech_judge = SpeechJudge(
            self.config.get_section("speech_judge"),
            queue=self.speech_queue,
        )

        # 5. 璇煶杈撳叆
        print("[5/5] 鍒濆鍖栬闊宠緭鍏?(ASR + Intent)...")
        self.voice = VoiceRuntime(self.config_data)

        # 娉ㄥ唽 GameAlarm 鍛戒护澶勭悊锛堥€氳繃浜嬩欢锛?
        self._register_alarm_events()

        # 娉ㄥ唽 Player Query 鍛戒护
        self._register_player_query()

        # 娉ㄥ唽 LoL 鑷姩鍖栵紙auto accept / auto banpick锛?
        self._register_automation()

        # 娉ㄥ唽鎵嬪姩鎶€鑳借鏃跺櫒锛坈ooldown锛?
        self._register_cooldown()

        # 娉ㄥ唽 OngoingGameService锛圕hampSelect 鍔犺浇 10 鐜╁锛?
        self._register_ongoing()

        print("\n鍒濆鍖栧畬鎴愶紒")

    def start(self):
        """doc"""
        if self._running:
            return

        self._running = True
        print("\n姝ｅ湪鍚姩绯荤粺...")

        # 鍚姩 Event Log锛堟渶鍏堝惎鍔級
        self.event_log.start()

        # 鍚姩璋冨害鍣?
        self.scheduler.start()

        # 鍚姩闊充箰妯″潡
        self.music.start()

        # 鎵弿闊充箰搴擄紙鍚庡彴锛?
        threading.Thread(target=self._scan_music_library, daemon=True).start()

        # 鍚姩 LoL 妯″潡
        self.lol.start()

        # 鍚姩 TTS
        self.tts.start()

        # 鍚姩 Speech Queue
        self.speech_queue.start()

        # 鍚姩 Speech Judge
        self.speech_judge.start()

        # 鍚姩璇煶杈撳叆
        self.voice.start()

        # 鍙戝竷鍚姩浜嬩欢
        self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_STARTUP,
            data={"timestamp": time.time()},
            source="system"
        ))

        self.state_store.set(StateNamespace.SYSTEM, "running", True)
        print("\nSystem started! Press Ctrl+C to stop.")
        print("-" * 60)

    def stop(self):
        """doc"""
        if not self._running:
            return

        print("\n姝ｅ湪鍋滄绯荤粺...")
        self._running = False

        # 鍙戝竷鍋滄浜嬩欢
        self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_SHUTDOWN,
            data={},
            source="system"
        ))

        # 鎸夐『搴忓仠姝?
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

        # 鏈€鍚庡仠姝?Event Log锛堢‘淇濇墍鏈夊仠姝簨浠堕兘琚褰曪級
        if self.event_log:
            self.event_log.stop()

        print("...")

    def _scan_music_library(self):
        """doc"""
        try:
            print("...")
            result = self.music.rescan()
            print(f"[Music] 鎵弿瀹屾垚: 鍏?{result.get('scanned', 0)} 涓枃浠? "
                  f"鏂板 {result.get('added', 0)}, 鏇存柊 {result.get('updated', 0)}")
        except Exception as e:
            print(f"[Music] 鎵弿澶辫触: {e}")

    def _register_alarm_events(self):
        """娉ㄥ唽 GameAlarm 浜嬩欢澶勭悊"""
        def on_create_flash(event: Event):
            champion = event.get("champion", "")
            if champion and self.lol:
                self.lol.game_alarm.create_flash_alarm(champion)

        def on_create_spell(event: Event):
            champion = event.get("champion", "")
            spell = event.get("spell", "")
            if champion and spell and self.lol:
                # 绠€鍖栵細榛樿鍐峰嵈 60 绉?
                self.lol.game_alarm.create_spell_alarm(champion, spell, cooldown=60)

        def on_create_ward(event: Event):
            location = event.get("location", "褰撳墠浣嶇疆")
            if self.lol:
                self.lol.game_alarm.create_ward_alarm(location)

        def on_create_custom(event: Event):
            title = event.get("title", "鎻愰啋")
            duration = event.get("duration", 120)
            if self.lol:
                self.lol.game_alarm.create_custom_alarm(title, duration)

        self.event_bus.subscribe("alarm.create_flash", on_create_flash)
        self.event_bus.subscribe("alarm.create_spell", on_create_spell)
        self.event_bus.subscribe("alarm.create_ward", on_create_ward)
        self.event_bus.subscribe("alarm.create_custom", on_create_custom)

    def _register_player_query(self):
        """doc"""
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
            print("...")
        except Exception as e:
            print(f"[PlayerQuery] 娉ㄥ唽澶辫触: {e}")

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
            print(f"[Automation] 宸叉敞鍐?enabled={cfg.enabled}")
        except Exception as e:
            print(f"[Automation] 娉ㄥ唽澶辫触: {e}")

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
                    pass

            def _on_event(code: str, data: dict):
                # 绠€鍗曟棩蹇楋紱TTS 鐢?_speech_enqueue 璐熻矗
                print(f"[Cooldown] {code}: {data.get('text') or data}")

            self.cooldown_manager = _CM(
                on_event=_on_event,
                speech_enqueue=_speech_enqueue,
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
            print("...")
        except Exception as e:
            print(f"[Cooldown] 娉ㄥ唽澶辫触: {e}")

    def _register_ongoing(self):
        try:
            from app.lol.ongoing.service import OngoingGameService
            self.ongoing_service = OngoingGameService(
                lcu=self.lol.lcu if self.lol else None,
                on_event=lambda code, data: print(f"[Ongoing] {code}: {data}"),
            )
            self.ongoing_service.start()
            print("...")
        except Exception as e:
            print(f"[Ongoing] 娉ㄥ唽澶辫触: {e}")

    def run(self):
        """doc"""
        self.initialize()
        self.start()

        def signal_handler(sig, frame):
            print("\n鏀跺埌鍋滄淇″彿...")
            self._shutdown_event.set()
            # UI 妯″紡涓嬶細璁?Qt 浜嬩欢寰幆閫€鍑猴紙headless 妯″紡 QApplication.instance() 涓?None锛?
            try:
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
                if app is not None:
                    app.quit()
            except Exception:
                pass

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # headless 妯″紡锛氬師闃诲寰幆
        if "--headless" in sys.argv:
            try:
                while not self._shutdown_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
            return

        # UI 妯″紡锛氫富绾跨▼璺?Qt锛圦Application 蹇呴』鍦ㄤ富绾跨▼鍒涘缓锛?
        try:
            from PySide6.QtWidgets import QApplication
            from app.ui.main_window import MainWindow
            from app.ui.tray import TrayIcon
        except Exception as e:
            print(f"[UI] PySide6 鏈畨瑁咃紝浣跨敤 headless: {e}")
            try:
                while not self._shutdown_event.is_set():
                    time.sleep(0.5)
            finally:
                self.stop()
            return

        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        try:
            from app.ui.theme.applier import apply_dark_theme
            apply_dark_theme(app)
        except Exception as _e:
            print(f'[UI] theme load failed: {_e}')

        print("[Run] creating MainWindow")
        window = MainWindow(
            automation_service=None,
            cooldown_manager=None,
            music=None,
        )
        window.resize(480, 640)
        window.show()
        window.raise_()
        window.activateWindow()
        print(f"[Run] MainWindow shown at {window.geometry()}")

        state = {"done": False, "error": None, "switched": False}

        def backend():
            try:
                print("[Run] background: initialize")
                self.initialize()
                print("[Run] background: start")
                self.start()
                print("[Run] background: done")
                state["done"] = True
            except Exception as e:
                import traceback
                traceback.print_exc()
                state["error"] = str(e)

        import threading
        threading.Thread(target=backend, daemon=True).start()

        from PySide6.QtCore import QTimer

        def check():
            if state["error"]:
                print(f"[Run] backend error: {state['error']}")
                return
            if state["done"] and not state["switched"]:
                state["switched"] = True
                print("[Run] switching to tabs")
                try:
                    self._switch_to_tabs(window)
                    print("[Run] switched")
                except Exception:
                    import traceback
                    traceback.print_exc()

        timer = QTimer()
        timer.timeout.connect(check)
        timer.start(500)
        self._startup_timer = timer

        print("[Run] entering app.exec()")
        try:
            app.exec()
        finally:
            self.stop()

    def _switch_to_tabs(self, window):
        print("[Run] _switch_to_tabs start")
        from app.ui.widgets.main_tabs import MainTabs
        print("[Run] building MainTabs")
        tabs = MainTabs(
            automation_service=getattr(self, "automation", None),
            cooldown_manager=getattr(self, "cooldown_manager", None),
            music=getattr(self, "music", None),
            ongoing_service=getattr(self, "ongoing_service", None),
        )
        print("[Run] MainTabs built, switching")
        window.switch_to_tabs(tabs)
        print("[Run] switched, showing tray")
        from app.ui.tray import TrayIcon
        tray = TrayIcon(window, on_quit=self.stop)
        tray.show()
        self._tray = tray
        self._main_window = window
        print("[Run] tray shown")


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
    app.run()


if __name__ == "__main__":
    main()

