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
        self._main_window = None  # LoadingPanel 状态灯接线（_set_module_status 用）

        # 鐘舵€?
        self._running = False
        self._shutdown_event = threading.Event()

    _initialized = False
    def _set_module_status(self, key: str, status: str):
        """更新 LoadingPanel 状态灯（跨线程安全：Signal queued；window 未就绪则跳过）"""
        w = getattr(self, "_main_window", None)
        if w is not None and hasattr(w, "_loading"):
            w._loading.status_changed.emit(key, status)

    def _ensure_asr_running(self):
        """自动拉起 ASR llama-server（如果端口 8080 未运行）"""
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
            print(f"[ASR] 端口 {port} 已在运行，直接连接")
            return

        # 需要启动 ASR
        model_path = asr_config.get("qwen3_asr_model", "")
        mmproj_path = asr_config.get("qwen3_asr_mmproj", "")

        # llama-server 路径
        llama_server = r"C:\Users\1\.lmstudio\extensions\backends\llama.cpp-win-x86_64-vulkan-avx2-2.41.0\llama-server.exe"
        if not os.path.exists(llama_server):
            print(f"[ASR] WARNING: llama-server 不存在: {llama_server}")
            print("[ASR] 请手动启动 ASR，或检查 llama-server 路径")
            return

        print(f"[ASR] 端口 {port} 未运行，正在自动拉起 llama-server...")
        print(f"[ASR] model: {model_path}")
        print(f"[ASR] mmproj: {mmproj_path}")

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
            subprocess.Popen(
                args,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=open("asr_server_out.txt", "w"),
                stderr=open("asr_server_err.txt", "w"),
            )
            print("[ASR] llama-server 已启动，等待就绪...")
        except Exception as e:
            print(f"[ASR] 启动 llama-server 失败: {e}")
            return

        # 等待 ASR 就绪（最多 60 秒）：轮询 /models 返回 200 才算就绪。
        # 端口占用 ≠ 就绪——llama-server 先监听端口再加载模型（模型未就绪时 /models 返回 503）
        import requests as _rq
        for i in range(60):
            time.sleep(1)
            try:
                resp = _rq.get(f"http://{host}:{port}/v1/models", timeout=1)
                if resp.status_code == 200:
                    print(f"[ASR] 就绪（等待 {i+1} 秒）")
                    return
            except Exception:
                pass
            if i % 10 == 0:
                print(f"[ASR] 等待中... ({i}s)")

        print("[ASR] 拉起超时（60秒），模型可能未就绪，继续启动")

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
            # demo/archive 为演示/归档源：默认禁用（网络慢且不稳定，会拖慢每次在线搜索与
            # 取 URL）。保留注册能力，需要时在 config.json 的 online_music 段设置
            # demo_source=true / archive_source=true 重新启用。
            _om_cfg = (self.config_data.get("online_music") or {})
            if _om_cfg.get("demo_source", False):
                self.online_music_manager.register(DemoSource())
                print("[Music] demo 音源已启用（online_music.demo_source=true）")
            if _om_cfg.get("archive_source", False):
                self.online_music_manager.register(ArchiveSource())
                print("[Music] archive 音源已启用（online_music.archive_source=true）")
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
                self._set_module_status("js", "ok")
            except Exception as e:
                print(f"[Music] JS bridge 鍒濆鍖栧け璐? {e}")
                self._set_module_status("js", "fail")
            self.music.set_online_manager(self.online_music_manager)
            # 同步「音源管理」UI 导入的自定义音源（config/music_sources.json）
            try:
                self.music.sync_custom_sources()
            except Exception as e:
                print(f"[Music] 自定义音源同步失败: {e}")
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

        # 敌方成装语音提醒（equipment_notifier，初始化失败不阻塞主流程）
        self._equip_enabled = True
        try:
            from app.lol.equipment_notifier import EquipmentUpdateNotifier
            self.equip_notifier = EquipmentUpdateNotifier(self.speech_queue)
            # 装备提醒持久化：启动读 ui_prefs.json（默认开）
            from app.ui.settings_store import load_ui_prefs
            self._equip_enabled = bool(load_ui_prefs().get("equip_notify", True))
        except Exception as _e:
            print(f"[Equipment] notifier 初始化失败: {_e}")
            self.equip_notifier = None

        # 5. 璇煶杈撳叆
        print("[5/5] 鍒濆鍖栬闊宠緭鍏?(ASR + Intent)...")
        self._set_module_status("asr", "loading")
        self._ensure_asr_running()
        self.voice = VoiceRuntime(self.config_data)
        self._set_module_status("asr", "ok")
        self._set_module_status("intent", "ok")

        # 娉ㄥ唽 GameAlarm 鍛戒护澶勭悊锛堥€氳繃浜嬩欢锛?
        self._register_alarm_events()

        # 娉ㄥ唽 Player Query 鍛戒护
        self._register_player_query()

        # 娉ㄥ唽 LoL 鑷姩鍖栵紙auto accept / auto banpick锛?
        self._register_automation()

        # 娉ㄥ唽鎵嬪姩鎶€鑳借鏃跺櫒锛坈ooldown锛?
        self._register_cooldown()

        # 娉ㄥ唽 OngoingGameService锛圕hampSelect 鍔犺浇 10 鐜╁锛?

        print("\n鍒濆鍖栧畬鎴愶紒")

    def start(self):
        """doc"""
        if self._running:
            return

        self._running = True
        print("\n姝ｅ湪鍚姩绯荤粺...")

        # 鍚姩 Event Log锛堟渶鍏堝惎鍔級
        self.event_log.start()
        self._set_module_status("eventlog", "ok")

        # 鍚姩璋冨害鍣?
        self.scheduler.start()

        # 鍚姩闊充箰妯″潡
        self.music.start()
        self._set_module_status("music", "ok")

        # 鎵弿闊充箰搴擄紙鍚庡彴锛?
        threading.Thread(target=self._scan_music_library, daemon=True).start()

        # 鍚姩 LoL 妯″潡
        self.lol.start()
        self._set_module_status("lol", "ok")

        # OngoingGameService 构造必须在 lcu discovery 之后：
        # 否则 _build_direct_getter / acct._direct 独立直连失效，退化为共享 lcu.get，
        # 游戏内共享连接断连窗口会导致 summoner/ranked/history 全部失败。
        self._register_ongoing()


        # 启动 OngoingGameService（ChampSelect 加载 10 玩家）
        if hasattr(self, 'ongoing_service') and self.ongoing_service:
            self.ongoing_service.start()
            print("[Ongoing] 加载服务已启动")

        # 注册一键连招 ComboManager（依赖 ongoing_service 识别当前英雄）
        self._register_combo()
        # 鍚姩 TTS
        self.tts.start()
        self._set_module_status("tts", "ok")

        # 鍚姩 Speech Queue
        self.speech_queue.start()
        self._set_module_status("speech", "ok")

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

        if getattr(self, "combo_manager", None):
            self.combo_manager.stop()

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
                pass

        def on_create_flash(event):
            print(f"[P0CD DEBUG] on_create_flash called: event_type={event.event_type}, data={event.data}")
            champion = event.get("champion", "")
            if not (champion and self.lol):
                print(f"[P0CD DEBUG] early return: champion={champion}, lol={self.lol}")
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
            print(f"[P0CD] raw={raw_champ} normalized={canonical} enemy_found={enemy is not None} "
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
            db_path = "data/player.db"
            events_dir = self.config.get(
                "events.log_dir", "./data/events"
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
                self._set_module_status("automation", "ok")
            else:
                self._set_module_status("automation", "disabled")
            print(f"[Automation] 宸叉敞鍐?enabled={cfg.enabled}")
        except Exception as e:
            print(f"[Automation] 娉ㄥ唽澶辫触: {e}")

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
            print("[Combo] ComboManager 已启动（默认关闭，需在自动化面板开启）")
        except Exception as e:
            print(f"[Combo] 注册失败: {e}")

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
                    print(f"[Cooldown] adjustment event error: {e}")
            
            self.event_bus.subscribe("cooldown.adjustment", _on_cooldown_adjustment)
            print("[Cooldown] adjustment event handler registered")
            
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
                print("[AxiomArc] evaluator registered")
            except Exception as _e:
                print(f"[AxiomArc] evaluator 注册失败: {_e}")
            print("...")
            # A5 经济状态边沿提醒（仅跨边界时一句短话，低噪音）
            try:
                from app.player.economy_advisor import make_economy_advisor
                def _snap_provider():
                    return self.lol.current_snapshot if self.lol else None
                adv = make_economy_advisor(_snap_provider, self.speech_queue, enabled=True)
                self.event_bus.subscribe("game.time_update", adv["on_time_update"])
                self.event_bus.subscribe("game.ended", adv["on_game_reset"])
                print("[A5] economy advisor registered")
            except Exception as _e:
                print(f"[A5] economy advisor 注册失败: {_e}")

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
                            print(f"[Equipment] 更新异常: {_e}")

                    def _on_equip_game_end(event):
                        try:
                            self.equip_notifier.clear()
                        except Exception as _e:
                            print(f"[Equipment] 清理异常: {_e}")

                    self.event_bus.subscribe("game.time_update", _on_equip_tick)
                    self.event_bus.subscribe("game.ended", _on_equip_game_end)
                    print("[Equipment] notifier registered")
            except Exception as _e:
                print(f"[Equipment] notifier 注册失败: {_e}")
        except Exception as e:
            print(f"[Cooldown] 娉ㄥ唽澶辫触: {e}")

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
                print(f"[Ongoing] {code}: {data}")

            print("[Ongoing] 高频日志去重已启用（session_source/gsm_supplement/live_merged 相同内容不重复打印）")
            self.ongoing_service = OngoingGameService(
                lcu=self.lol.lcu if self.lol else None,
                on_event=_ongoing_on_event,
                account_history_service=acct,
                recent_n=20,
                snapshot_provider=_snap_for_ongoing,
                gsm_provider=gsm,
            )
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
        self._main_window = window
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
                    # H.4.4: 启动 Overlay（在主线程中）
                    self._start_overlay()
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

    def _start_overlay(self):
        try:
            from app.ui.overlay.overlay_model import OverlayModel
            from app.ui.overlay.overlay_window import OverlayWindow
            cm = getattr(self, "cooldown_manager", None)
            def phase():
                try:
                    # 真实读取LCU游戏阶段，不是靠is_game_active硬判断
                    lol = getattr(self, "lol", None)
                    if lol and hasattr(lol, "lcu") and lol.lcu:
                        p = lol.lcu.get("/lol-gameflow/v1/gameflow-phase")
                        if p == "InProgress":
                            return "InProgress"
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
                print(f"[Overlay] spell clicked: champion={champion_name}, spell={spell_name}")
                # 规范化 spell 名称：把 2999 的变体名称转换成 canonical name
                # 例如："治疗术" → "治疗"，"解封的传送" → "传送"，"引燃" → "点燃"
                from app.ui.overlay.overlay_model import _normalize_spell_name
                from app.lol.data.summoner_spell_catalog import SummonerSpellCatalog
                _spell_catalog = SummonerSpellCatalog()
                canonical_spell = _normalize_spell_name(spell_name, _spell_catalog)
                print(f"[Overlay] normalized spell: {spell_name} → {canonical_spell}")
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
            print("[Run] overlay started")
        except Exception:
            import traceback; traceback.print_exc()
    def _switch_to_tabs(self, window):
        print("[Run] _switch_to_tabs start")
        from app.ui.widgets.main_tabs import MainTabs
        print("[Run] building MainTabs")
        tabs = MainTabs(
            automation_service=getattr(self, "automation", None),
            cooldown_manager=getattr(self, "cooldown_manager", None),
            music=getattr(self, "music", None),
            ongoing_service=getattr(self, "ongoing_service", None),
            snapshot_provider=lambda: (self.lol.current_snapshot if getattr(self, "lol", None) else None),
            combo_manager=getattr(self, "combo_manager", None),
            services_dict={"lcu": getattr(self.lol, "lcu", None) if getattr(self, "lol", None) else None},
            tts=getattr(self, "tts", None),
            equip_toggle=lambda on: setattr(self, "_equip_enabled", bool(on)),
            overlay_toggle=window._on_overlay_toggle,
            overlay_enabled=True,
                    )
        print("[Run] MainTabs built, switching")
        # 统一运行时状态：MainWindow 底部唯一 StatusBar 读取这套真实服务
        try:
            window._services = {
                "lcu": getattr(getattr(self, "automation", None), "_lcu", None),
                "asr": getattr(getattr(self, "voice", None), "asr", None),
                "intent": getattr(getattr(self, "voice", None), "intent_router", None),
                "voice": getattr(self, "voice", None),
            }
        except Exception as e:
            print(f"[UI] services wiring failed: {e}")
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





