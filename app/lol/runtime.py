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
from .objective_timer import ObjectiveTimer
from .lcu import LCUConnection
from ..player import PlayerRepository, PlayerAnalysisService
from ..player.match_repository import MatchRepository


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
        self.objective_timer = ObjectiveTimer(self.game_alarm)
        self.player_repo = PlayerRepository("data/player.db")
        self.player_analysis = PlayerAnalysisService(self.player_repo, self.lcu)
        self.match_repo = MatchRepository("data/player.db")
        self._local_match_id: Optional[str] = None

        # 核心服务
        self.event_bus = get_event_bus()
        self.scheduler = get_scheduler()
        self.state_store = get_state_store()

        # 状态
        self._running = False
        self._current_snapshot: Optional[GameSnapshot] = None
        self._game_active = False

        # LCU GameFlow 状态
        self._last_lcu_phase: Optional[str] = None
        self._lcu_was_connected = False

        # LCU Summoner 状态
        self._last_summoner: Optional[Dict[str, Any]] = None

        # LCU Champ Select 状态
        self._last_champ_select: Optional[Dict[str, Any]] = None
        self._champ_select_active: bool = False

        # GameEnd 缓存和 grace window
        self._cached_game_end_result: Optional[str] = None
        self._game_end_published: bool = False
        self._game_end_retry_count: int = 0
        self._max_game_end_retries: int = 2
        self._game_end_retry_delay: float = 0.5

    def start(self):
        """启动 LoL 运行时"""
        if self._running:
            return

        self._running = True

        # 启动 GameAlarm
        self.game_alarm.start()
        self.objective_timer.start()

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
            interval=1.0,
        )

        print("[LoL] 运行时已启动")

    def stop(self):
        """停止 LoL 运行时"""
        self._running = False
        self.objective_timer.stop()
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
                # 游戏可能结束，进入 grace window 确认阶段
                self._handle_game_end_detection()
            return

        # 游戏正在运行
        if not self._game_active:
            # 新游戏开始，清除 GameEnd 缓存和标记
            self._cached_game_end_result = None
            self._game_end_published = False
            self._game_end_retry_count = 0
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

            # Player Analysis (read-only, no speech)
            try:
                self.player_analysis.analyze_game(snapshot)
            except Exception as e:
                print(f"[PlayerAnalysis] 分析失败: {e}")

            # 更新状态存储
            self._update_state(snapshot)

            # 读取 eventdata，缓存 GameEnd
            try:
                eventdata = self.live_api.get_event_data()
                if eventdata and 'Events' in eventdata:
                    for evt in eventdata['Events']:
                        if evt.get('EventName') == 'GameEnd':
                            self._cached_game_end_result = evt.get('Result', '')
                            break
            except Exception:
                pass

            # 检测状态差异
            events = self.state_diff.diff(snapshot)

            # 发布事件
            for event in events:
                # 检查是否是 GAME_ENDED 事件
                if event.event_type == EventType.GAME_ENDED:
                    if self._game_end_published:
                        continue  # 已经发布过，跳过重复
                    self._game_end_published = True
                    # 如果缓存中有 result，优先使用
                    if self._cached_game_end_result and not event.data.get('result'):
                        event.data['result'] = self._cached_game_end_result

                # GAME_STARTED → 创建 local_match_id
                if event.event_type == EventType.GAME_STARTED:
                    import uuid
                    self._local_match_id = str(uuid.uuid4())
                    event.data["local_match_id"] = self._local_match_id
                    try:
                        self.match_repo.create_match(
                            self._local_match_id,
                            start_time=time.time(),
                            game_mode=snapshot.game_mode,
                            map_name=snapshot.map_name,
                        )
                    except Exception as e:
                        print(f"[MatchHistory] create_match 失败: {e}")

                # 注入 local_match_id 到所有事件
                if self._local_match_id and event.event_type != EventType.GAME_STARTED:
                    event.data.setdefault("local_match_id", self._local_match_id)

                # GAME_ENDED → 持久化 match players
                if event.event_type == EventType.GAME_ENDED:
                    self._persist_match_end(event)

                self.event_bus.publish(Event(
                    event_type=event.event_type,
                    data=event.data,
                    source="lol"
                ))

        except Exception as e:
            print(f"[LoL] 数据轮询异常: {e}")

    def _handle_game_end_detection(self):
        """
        游戏结束检测：grace window 重试机制

        is_available() 失败后，不要立即发布 GAME_ENDED。
        短暂重试几次，尝试拿到 GameEnd.Result。
        """
        if self._game_end_published:
            # 已经发布过 GAME_ENDED，不需要再处理
            return

        # grace window 重试
        if self._game_end_retry_count < self._max_game_end_retries:
            self._game_end_retry_count += 1
            # 短暂等待后再试（在当前轮询周期内，下一次轮询会自动重试）
            # 这里不阻塞，让 scheduler 下一次轮询再检测
            return

        # 所有重试都失败，最终确认游戏结束
        self._game_active = False
        self.state_diff.reset()

        # 构造 GAME_ENDED 事件，带上缓存的 result（如果有）
        game_end_data = {}
        if self._cached_game_end_result:
            game_end_data['result'] = self._cached_game_end_result

        self.event_bus.publish(Event(
            event_type=EventType.GAME_ENDED,
            data=game_end_data,
            source="lol"
        ))

        self._game_end_published = True
        self.state_store.set(StateNamespace.GAME, "is_active", False)

    def _persist_match_end(self, event):
        """GameEnd 时持久化 MatchRecord + MatchPlayer"""
        if not self._local_match_id:
            return
        mid = self._local_match_id
        try:
            result = event.data.get("result", "Unknown")
            snapshot = self._current_snapshot
            duration = snapshot.game_time if snapshot else 0.0
            end_time = time.time()
            self.match_repo.finish_match(mid, end_time=end_time, duration=duration, result=result)

            players_data = []
            if snapshot and snapshot.players:
                self_puuid = ""
                if self.lcu and self.lcu.connected:
                    summ = self.lcu.get_current_summoner()
                    if summ:
                        self_puuid = summ.get("puuid", "")
                for p in snapshot.players:
                    players_data.append({
                        "puuid": self_puuid if p.is_current_player else None,
                        "riot_id": p.riot_id or p.summoner_name,
                        "team": p.team,
                        "champion_name": p.champion_name,
                        "champion_id": p.champion_id,
                        "position": p.position,
                        "kills": p.kills,
                        "deaths": p.deaths,
                        "assists": p.assists,
                        "gold": p.gold,
                        "is_self": p.is_current_player,
                    })
            if players_data:
                self.match_repo.save_match_players(mid, players_data)
            print(f"[MatchHistory] match {mid[:8]} persisted: {len(players_data)} players, result={result}, duration={duration:.0f}s")
        except Exception as e:
            print(f"[MatchHistory] persist 失败: {e}")
        finally:
            self._local_match_id = None

    def _poll_lcu(self):
        """轮询 LCU 状态，检测 GameFlow 变化并发布事件"""
        if not self._running:
            return

        if not self.lcu.connected:
            # LCU 断开
            if self._lcu_was_connected:
                self._lcu_was_connected = False
                self._last_lcu_phase = None
                self._last_summoner = None
                self._last_champ_select = None
                self._champ_select_active = False
                self.state_store.set(StateNamespace.LCU, "connected", False)
                self.event_bus.publish(Event(
                    event_type=EventType.LCU_DISCONNECTED,
                    data={},
                    source="lol"
                ))
            return

        # LCU 刚连接
        if not self._lcu_was_connected:
            self._lcu_was_connected = True
            self.state_store.set(StateNamespace.LCU, "connected", True)
            self.event_bus.publish(Event(
                event_type=EventType.LCU_CONNECTED,
                data={},
                source="lol"
            ))

        # 获取当前 phase
        try:
            phase = self.lcu.get_gameflow_phase()
        except Exception as e:
            print(f"[LCU] 获取 gameflow phase 失败: {e}")
            return

        if not phase:
            return

        # 归类未知 phase
        known_phases = {
            "None", "Lobby", "ChampSelect", "GameStart",
            "InProgress", "Reconnecting", "WaitingForStats",
            "PreEndOfGame", "EndOfGame", "Tournament"
        }
        normalized_phase = phase if phase in known_phases else "Unknown"

        # 更新状态存储
        self.state_store.set(StateNamespace.LCU, "gameflow_phase", normalized_phase)
        self.state_store.set(StateNamespace.LCU, "raw_phase", phase)

        # 检测变化
        if self._last_lcu_phase is None:
            # 第一次获取，发布初始状态
            self._last_lcu_phase = normalized_phase
            self.event_bus.publish(Event(
                event_type=EventType.LCU_GAMEFLOW_CHANGED,
                data={
                    "old_phase": "UNKNOWN",
                    "new_phase": normalized_phase,
                    "raw_phase": phase,
                },
                source="lol"
            ))
        elif self._last_lcu_phase != normalized_phase:
            # phase 变化
            old_phase = self._last_lcu_phase
            self._last_lcu_phase = normalized_phase
            self.event_bus.publish(Event(
                event_type=EventType.LCU_GAMEFLOW_CHANGED,
                data={
                    "old_phase": old_phase,
                    "new_phase": normalized_phase,
                    "raw_phase": phase,
                },
                source="lol"
            ))

        # 获取 Champ Select Session
        self._poll_champ_select()

        # 获取 current-summoner
        try:
            summoner = self.lcu.get_current_summoner()
        except Exception as e:
            print(f"[LCU] 获取 current-summoner 失败: {e}")
            summoner = None

        if not summoner or not isinstance(summoner, dict):
            return

        # 提取有用字段（兼容 Tencent 可能缺失的字段）
        summoner_info = {
            "summonerId": summoner.get("summonerId"),
            "displayName": summoner.get("displayName"),
            "profileIconId": summoner.get("profileIconId"),
            "summonerLevel": summoner.get("summonerLevel"),
            "puuid": summoner.get("puuid"),
            "gameName": summoner.get("gameName"),
            "tagLine": summoner.get("tagLine"),
        }

        # 更新 StateStore
        self.state_store.set(StateNamespace.LCU, "current_summoner", summoner_info)

        # 检测变化（只比较关键字段）
        new_key = (
            summoner_info.get("summonerId"),
            summoner_info.get("displayName"),
            summoner_info.get("summonerLevel"),
        )

        if self._last_summoner is None:
            # 第一次获取
            self._last_summoner = summoner_info
            self.event_bus.publish(Event(
                event_type=EventType.LCU_SUMMONER_UPDATED,
                data={
                    "summoner": summoner_info,
                    "is_first": True,
                },
                source="lol"
            ))
        else:
            old_key = (
                self._last_summoner.get("summonerId"),
                self._last_summoner.get("displayName"),
                self._last_summoner.get("summonerLevel"),
            )
            if new_key != old_key:
                # 数据变化
                self._last_summoner = summoner_info
                self.event_bus.publish(Event(
                    event_type=EventType.LCU_SUMMONER_UPDATED,
                    data={
                        "summoner": summoner_info,
                        "is_first": False,
                    },
                    source="lol"
                ))

    def _poll_champ_select(self):
        """轮询 Champ Select Session，检测状态变化并发布事件"""
        if not self._running:
            return

        try:
            session = self.lcu.get_champ_select_session()
        except Exception as e:
            print(f"[LCU] 获取 champ select session 失败: {e}")
            session = None

        # 404 / None = NO_SESSION
        if not session or not isinstance(session, dict):
            if self._champ_select_active:
                # 从 ACTIVE → NO_SESSION
                self._champ_select_active = False
                self._last_champ_select = None
                self.state_store.set(StateNamespace.LCU, "champ_select", {"status": "NO_SESSION"})
                self.event_bus.publish(Event(
                    event_type=EventType.LCU_CHAMP_SELECT_CHANGED,
                    data={
                        "status": "ENDED",
                    },
                    source="lol"
                ))
            return

        # ACTIVE_SESSION：提取 normalized state
        normalized = self._normalize_champ_select(session)

        # 更新 StateStore
        self.state_store.set(StateNamespace.LCU, "champ_select", normalized)

        # 检测变化
        if not self._champ_select_active:
            # 从 NO_SESSION → ACTIVE
            self._champ_select_active = True
            self._last_champ_select = normalized
            self.event_bus.publish(Event(
                event_type=EventType.LCU_CHAMP_SELECT_CHANGED,
                data={
                    "status": "STARTED",
                    "session": normalized,
                },
                source="lol"
            ))
        else:
            # 比较关键字段
            new_key = self._champ_select_key(normalized)
            old_key = self._champ_select_key(self._last_champ_select) if self._last_champ_select else None
            if new_key != old_key:
                self._last_champ_select = normalized
                self.event_bus.publish(Event(
                    event_type=EventType.LCU_CHAMP_SELECT_CHANGED,
                    data={
                        "status": "UPDATED",
                        "session": normalized,
                    },
                    source="lol"
                ))

    def _normalize_champ_select(self, session: Dict[str, Any]) -> Dict[str, Any]:
        """
        从原始 Champ Select session JSON 提取 V1 有用字段

        只提取后续真正需要的最小结构，不保存整个巨大 JSON。
        """
        normalized = {
            "status": "ACTIVE_SESSION",
            "phase": session.get("phase"),
            "timer": None,
            "local_player": None,
            "my_team_champions": [],
            "enemy_team_champions": [],
        }

        # 计时器信息
        timer = session.get("timer")
        if isinstance(timer, dict):
            normalized["timer"] = {
                "phase": timer.get("phase"),
                "adjustedTimeLeftInPhase": timer.get("adjustedTimeLeftInPhase"),
                "totalTimeInPhase": timer.get("totalTimeInPhase"),
            }

        # 本地玩家信息
        local_player = session.get("localPlayerCellId")
        if local_player is not None:
            normalized["local_player"] = {"cellId": local_player}

        # 队伍信息
        my_team = session.get("myTeam", [])
        if isinstance(my_team, list):
            for player in my_team:
                if isinstance(player, dict):
                    cell_id = player.get("cellId")
                    champion_id = player.get("championId")
                    champion_pick = player.get("championPickIntent")
                    is_picked = player.get("isPicked")
                    is_banned = player.get("isBanned")
                    if cell_id is not None and (champion_id or is_picked or champion_pick):
                        normalized["my_team_champions"].append({
                            "cellId": cell_id,
                            "championId": champion_id,
                            "championPickIntent": champion_pick,
                            "isPicked": is_picked,
                            "isBanned": is_banned,
                        })

        # 敌方队伍
        enemy_team = session.get("enemyTeam", [])
        if isinstance(enemy_team, list):
            for player in enemy_team:
                if isinstance(player, dict):
                    champion_id = player.get("championId")
                    is_banned = player.get("isBanned")
                    if champion_id or is_banned:
                        normalized["enemy_team_champions"].append({
                            "championId": champion_id,
                            "isBanned": is_banned,
                        })

        return normalized

    def _champ_select_key(self, state: Optional[Dict[str, Any]]) -> tuple:
        """
        生成用于比较的 key（比较所有真正影响 V1 Champ Select 状态的字段）

        保持 deterministic 顺序：队伍成员按 cellId 排序。
        不比较 timer.adjustedTimeLeftInPhase（每秒变化，避免 spam）。
        比较 timer.phase（阶段变化应该触发事件）。
        """
        if state is None:
            return None

        # 本地玩家
        local_player = state.get("local_player") or {}
        local_cell_id = local_player.get("cellId")

        # 我方队伍：按 cellId 排序，提取每个成员的关键字段
        my_team = state.get("my_team_champions", [])
        my_team_sorted = sorted(my_team, key=lambda x: x.get("cellId", 0))
        my_team_key = tuple(
            (
                member.get("cellId"),
                member.get("championId"),
                member.get("championPickIntent"),
                member.get("isPicked"),
                member.get("isBanned"),
            )
            for member in my_team_sorted
        )

        # 敌方队伍：按 championId 排序（没有 cellId）
        enemy_team = state.get("enemy_team_champions", [])
        enemy_team_sorted = sorted(enemy_team, key=lambda x: x.get("championId", 0))
        enemy_team_key = tuple(
            (
                member.get("championId"),
                member.get("isBanned"),
            )
            for member in enemy_team_sorted
        )

        # timer.phase（阶段变化触发事件，但倒计时不触发）
        timer = state.get("timer") or {}
        timer_phase = timer.get("phase")

        return (
            state.get("phase"),
            timer_phase,
            local_cell_id,
            my_team_key,
            enemy_team_key,
        )

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
