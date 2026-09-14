"""
LoL 运行时 - 整合 2999 数据读取、快照、状态差异、GameAlarm

负责：
- 定期轮询 2999 API
- 构建 GameSnapshot
- 检测状态差异产生事件
- 管理 GameAlarm
- 通过 EventBus 发布所有游戏事件
"""
import threading
import time
from typing import Any, Dict, Optional

from ..core.event_bus import EventBus, Event, EventType, get_event_bus
from ..core.scheduler import Scheduler, get_scheduler
from ..core.state_store import StateStore, StateNamespace, get_state_store

from .live_client import LiveClientAPI
from .snapshot import GameSnapshot, SnapshotBuilder
from .state_diff import StateDiff
from .game_alarm import GameAlarmEngine
from .lcu import LCUConnection


class LoLRuntime:
    """
    LoL 游戏运行时

    整合所有 LoL 相关子系统。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.live_config = config.get("live_client", {})
        self.lcu_config = config.get("lcu", {})
        self.alarm_config = config.get("game_alarm", {})

        # 子系统
        self.live_api = LiveClientAPI(
            base_url=self.live_config.get("base_url", "https://127.0.0.1:2999"),
            verify_ssl=self.live_config.get("verify_ssl", False),
        )
        self.lcu = LCUConnection(
            process_name=self.lcu_config.get("process_name", "LeagueClientUx.exe"),
            reconnect_interval=self.lcu_config.get("reconnect_interval", 5),
        )
        self.state_diff = StateDiff()
        self.game_alarm = GameAlarmEngine(self.alarm_config)

        # 核心服务
        self.event_bus = get_event_bus()
        self.scheduler = get_scheduler()
        self.state_store = get_state_store()

        # 状态
        self._running = False
        self._current_snapshot: Optional[GameSnapshot] = None
        self._game_active = False

    def start(self):
        """启动 LoL 运行时"""
        if self._running:
            return

        self._running = True

        # 启动 GameAlarm
        self.game_alarm.start()

        # 启动 LCU
        if self.lcu_config.get("enabled", True):
            self.lcu.start()

        # 注册定时任务：轮询游戏数据
        poll_interval = self.live_config.get("poll_interval", 1.0)
        self.scheduler.add_task(
            "lol_poll_game",
            self._poll_game_data,
            interval=poll_interval,
        )

        # 注册 LCU 状态轮询
        self.scheduler.add_task(
            "lol_poll_lcu",
            self._poll_lcu,
            interval=2.0,
        )

        print("[LoL] 运行时已启动")

    def stop(self):
        """停止 LoL 运行时"""
        self._running = False
        self.game_alarm.stop()
        self.lcu.stop()
        self.live_api.close()
        print("[LoL] 运行时已停止")

    def _poll_game_data(self):
        """轮询游戏数据"""
        if not self._running:
            return

        # 检查 API 是否可用
        if not self.live_api.is_available():
            if self._game_active:
                # 游戏结束
                self._game_active = False
                self.state_diff.reset()
                self.event_bus.publish(Event(
                    event_type=EventType.GAME_ENDED,
                    data={},
                    source="lol"
                ))
                self.state_store.set(StateNamespace.GAME, "is_active", False)
            return

        # 游戏正在运行
        if not self._game_active:
            self._game_active = True
            self.state_store.set(StateNamespace.GAME, "is_active", True)

        # 获取数据
        try:
            all_data = self.live_api.get_all_game_data()
            if not all_data:
                return

            # 构建快照
            snapshot = SnapshotBuilder.from_api_data(all_data)
            self._current_snapshot = snapshot

            # 更新状态存储
            self._update_state(snapshot)

            # 检测状态差异
            events = self.state_diff.diff(snapshot)

            # 发布事件
            for event in events:
                self.event_bus.publish(Event(
                    event_type=event.event_type,
                    data=event.data,
                    source="lol"
                ))

        except Exception as e:
            print(f"[LoL] 数据轮询异常: {e}")

    def _poll_lcu(self):
        """轮询 LCU 状态"""
        if not self._running:
            return

        if self.lcu.connected:
            phase = self.lcu.get_gameflow_phase()
            self.state_store.set(StateNamespace.LCU, "gameflow_phase", phase)
            self.state_store.set(StateNamespace.LCU, "connected", True)
        else:
            self.state_store.set(StateNamespace.LCU, "connected", False)

    def _update_state(self, snapshot: GameSnapshot):
        """更新状态存储"""
        self.state_store.set(StateNamespace.GAME, "game_time", snapshot.game_time)
        self.state_store.set(StateNamespace.GAME, "game_mode", snapshot.game_mode)
        self.state_store.set(StateNamespace.GAME, "map_name", snapshot.map_name)

        if snapshot.current_player:
            self.state_store.set(StateNamespace.GAME, "current_player", {
                "summoner_name": snapshot.current_player.summoner_name,
                "champion_name": snapshot.current_player.champion_name,
                "level": snapshot.current_player.level,
                "kills": snapshot.current_player.kills,
                "deaths": snapshot.current_player.deaths,
                "assists": snapshot.current_player.assists,
                "cs": snapshot.current_player.cs,
            })

        self.state_store.set(StateNamespace.GAME, "players", [
            {
                "summoner_name": p.summoner_name,
                "champion_name": p.champion_name,
                "team": p.team,
                "level": p.level,
                "kills": p.kills,
                "deaths": p.deaths,
                "assists": p.assists,
            }
            for p in snapshot.players
        ])

    @property
    def current_snapshot(self) -> Optional[GameSnapshot]:
        return self._current_snapshot

    @property
    def is_game_active(self) -> bool:
        return self._game_active
