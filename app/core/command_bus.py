"""
命令总线 - 处理用户/系统发出的操作命令

与 EventBus 的区别：
- Event: 发生了什么（事实）
- Command: 要做什么（动作）

Command -> 执行器 -> 产生 Event
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Command:
    """通用命令"""
    command_type: str  # 如 "music.next", "alarm.create_flash"
    params: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # 命令来源（voice, ui, system）
    require_confirmation: bool = False  # 是否需要确认

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


# 命令类型常量
class CommandType:
    # ===== 音乐命令 =====
    MUSIC_PLAY = "music.play"
    MUSIC_PAUSE = "music.pause"
    MUSIC_RESUME = "music.resume"
    MUSIC_TOGGLE = "music.toggle"
    MUSIC_NEXT = "music.next"
    MUSIC_PREV = "music.previous"
    MUSIC_VOLUME_SET = "music.volume_set"
    MUSIC_VOLUME_UP = "music.volume_up"
    MUSIC_VOLUME_DOWN = "music.volume_down"
    MUSIC_SEEK = "music.seek"
    MUSIC_SEARCH_PLAY = "music.search_play"
    MUSIC_HIGHLIGHT = "music.highlight"
    MUSIC_HIGHLIGHT_MODE = "music.highlight_mode"
    MUSIC_SET_PLAY_MODE = "music.set_play_mode"
    MUSIC_RESCAN = "music.rescan"
    MUSIC_CLEANUP = "music.cleanup"
    MUSIC_CLEAR_QUEUE = "music.clear_queue"
    # 停止当前歌曲（保留队列；语音"停止音乐"等使用，不经过电影路由、不触发清空队列）
    MUSIC_STOP = "music.stop"
    # 哼唱识曲（听歌识曲；UI 层注册，语音/按钮触发）
    MUSIC_IDENTIFY = "music.identify"

    # ===== 电影命令（包2b：语音控制电影） =====
    MOVIE_SEARCH_PLAY = "movie.search_play"
    MOVIE_PAUSE = "movie.pause"
    MOVIE_RESUME = "movie.resume"
    MOVIE_NEXT_EPISODE = "movie.next_episode"
    MOVIE_PREV_EPISODE = "movie.prev_episode"

    # ===== GameAlarm 命令 =====
    ALARM_CREATE_FLASH = "alarm.create_flash"
    ALARM_CREATE_SPELL = "alarm.create_spell"
    ALARM_CREATE_WARD = "alarm.create_ward"
    ALARM_CREATE_CUSTOM = "alarm.create_custom"
    ALARM_CANCEL = "alarm.cancel"
    ALARM_LIST = "alarm.list"

    # ===== 语音命令 =====
    VOICE_ENABLE = "voice.enable"
    VOICE_DISABLE = "voice.disable"
    TTS_ENABLE = "tts.enable"
    TTS_DISABLE = "tts.disable"

    # ===== 自动操作命令 =====
    AUTO_ACCEPT_TOGGLE = "auto.accept_toggle"
    AUTO_BANPICK_TOGGLE = "auto.banpick_toggle"

    # ===== Cooldown 手动计时器 =====
    COOLDOWN_START = "cooldown.start"
    COOLDOWN_QUERY = "cooldown.query"
    COOLDOWN_CANCEL = "cooldown.cancel"
    COOLDOWN_CANCEL_ALL = "cooldown.cancel_all"
    COOLDOWN_LIST = "cooldown.list"

    # ===== Player Query =====
    PLAYER_QUERY = "player.query"

    # ===== 系统命令 =====
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_RESTART = "system.restart"


class CommandBus:
    """
    命令总线

    每个命令类型注册一个执行器。
    支持中间件（日志、权限、确认等）。
    """

    def __init__(self):
        self._handlers: dict[str, Callable[[Command], Any]] = {}
        self._middlewares: list[Callable[[Command], bool | None]] = []
        self._lock = threading.Lock()
        self._command_history: list[Command] = []

    def register(self, command_type: str, handler: Callable[[Command], Any]):
        """注册命令处理器"""
        with self._lock:
            self._handlers[command_type] = handler

    def unregister(self, command_type: str):
        """注销命令处理器"""
        with self._lock:
            self._handlers.pop(command_type, None)

    def add_middleware(self, middleware: Callable[[Command], bool | None]):
        """
        添加中间件
        中间件返回 False 则中止命令执行
        返回 None 或 True 则继续
        """
        with self._lock:
            self._middlewares.append(middleware)

    def execute(self, command: Command) -> Any:
        """
        执行命令

        Returns:
            命令执行结果
        """
        # 记录历史
        with self._lock:
            self._command_history.append(command)

        # 运行中间件
        for middleware in self._middlewares:
            try:
                result = middleware(command)
                if result is False:
                    print(f"[CommandBus] 命令被中间件中止: {command.command_type}")
                    return None
            except Exception as e:
                # fail-closed：中间件异常视为拒绝执行，绝不静默放行
                print(f"[CommandBus] 中间件异常，命令中止: {command.command_type}: {e}")
                logger.error("中间件异常，命令中止 %s: %s", command.command_type, e)
                try:
                    from .event_bus import Event, EventType, get_event_bus
                    get_event_bus().publish(Event(
                        event_type=EventType.SYSTEM_ERROR,
                        data={
                            "source": "command_bus",
                            "command_type": command.command_type,
                            "error_type": type(e).__name__,
                            "error_msg": str(e),
                        },
                        source="command_bus",
                    ))
                except Exception:
                    logger.error("发布中间件 SYSTEM_ERROR 失败", exc_info=True)
                return None

        # 查找处理器
        with self._lock:
            handler = self._handlers.get(command.command_type)

        if handler is None:
            print(f"[CommandBus] 未注册的命令: {command.command_type}")
            return None

        # 执行
        try:
            return handler(command)
        except Exception as e:
            print(f"[CommandBus] 命令执行异常 {command.command_type}: {e}")
            raise

    def execute_async(self, command: Command):
        """异步执行命令（子线程异常被捕获记录，不静默死）"""

        def _run():
            try:
                self.execute(command)
            except Exception as e:
                print(f"[CommandBus] 异步命令执行异常 {command.command_type}: {e}")
                logger.error("异步命令执行异常 %s: %s", command.command_type, e)

        thread = threading.Thread(
            target=_run,
            daemon=True,
            name=f"cmd-{command.command_type}"
        )
        thread.start()

    def has_handler(self, command_type: str) -> bool:
        """检查命令是否有处理器"""
        with self._lock:
            return command_type in self._handlers

    def get_history(self, limit: int = 100) -> list[Command]:
        """获取命令历史"""
        with self._lock:
            return list(self._command_history[-limit:])


# 全局命令总线单例
_command_bus: CommandBus | None = None


def get_command_bus() -> CommandBus:
    """获取全局命令总线单例"""
    global _command_bus
    if _command_bus is None:
        _command_bus = CommandBus()
    return _command_bus
