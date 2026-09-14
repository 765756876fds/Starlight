"""
命令总线 - 处理用户/系统发出的操作命令

与 EventBus 的区别：
- Event: 发生了什么（事实）
- Command: 要做什么（动作）

Command -> 执行器 -> 产生 Event
"""
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Command:
    """通用命令"""
    command_type: str  # 如 "music.next", "alarm.create_flash"
    params: Dict[str, Any] = field(default_factory=dict)
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
    MUSIC_RESCAN = "music.rescan"

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
        self._handlers: Dict[str, Callable[[Command], Any]] = {}
        self._middlewares: List[Callable[[Command], Optional[bool]]] = []
        self._lock = threading.Lock()
        self._command_history: List[Command] = []

    def register(self, command_type: str, handler: Callable[[Command], Any]):
        """注册命令处理器"""
        with self._lock:
            self._handlers[command_type] = handler

    def unregister(self, command_type: str):
        """注销命令处理器"""
        with self._lock:
            self._handlers.pop(command_type, None)

    def add_middleware(self, middleware: Callable[[Command], Optional[bool]]):
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
                print(f"[CommandBus] 中间件异常: {e}")

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
        """异步执行命令"""
        thread = threading.Thread(
            target=self.execute,
            args=(command,),
            daemon=True,
            name=f"cmd-{command.command_type}"
        )
        thread.start()

    def has_handler(self, command_type: str) -> bool:
        """检查命令是否有处理器"""
        with self._lock:
            return command_type in self._handlers

    def get_history(self, limit: int = 100) -> List[Command]:
        """获取命令历史"""
        with self._lock:
            return list(self._command_history[-limit:])


# 全局命令总线单例
_command_bus: Optional[CommandBus] = None


def get_command_bus() -> CommandBus:
    """获取全局命令总线单例"""
    global _command_bus
    if _command_bus is None:
        _command_bus = CommandBus()
    return _command_bus
