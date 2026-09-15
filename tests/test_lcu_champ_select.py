"""
LCU Champ Select Session 测试
"""
import pytest
from unittest.mock import MagicMock, patch

from app.core.event_bus import EventBus, Event, EventType
from app.lol.runtime import LoLRuntime


class MockLCU:
    """Mock LCU 连接"""
    def __init__(self):
        self.connected = False
        self._phase = "None"
        self._summoner = {"summonerId": 123, "displayName": "Test", "summonerLevel": 30}
        self._champ_session = None

    def get_gameflow_phase(self):
        return self._phase

    def get_current_summoner(self):
        return self._summoner

    def get_champ_select_session(self):
        return self._champ_session

    def start(self):
        pass

    def stop(self):
        pass


class TestLcuChampSelect:
    """LCU Champ Select 测试"""

    def setup_method(self):
        """每个测试前初始化"""
        self.event_bus = EventBus()
        self.events_received = []

        self.event_bus.subscribe(EventType.LCU_CHAMP_SELECT_CHANGED, self._on_event)

        with patch('app.lol.runtime.LiveClientAPI'), \
             patch('app.lol.runtime.GameAlarmEngine'), \
             patch('app.lol.runtime.SnapshotBuilder'), \
             patch('app.lol.runtime.StateDiff'):
            self.runtime = LoLRuntime({})

        self.mock_lcu = MockLCU()
        self.runtime.lcu = self.mock_lcu
        self.runtime.event_bus = self.event_bus
        self.runtime._running = True

    def _on_event(self, event: Event):
        self.events_received.append(event)

    def _get_champ_events(self):
        return [e for e in self.events_received if e.event_type == EventType.LCU_CHAMP_SELECT_CHANGED]

    def _make_session(self, phase="Pick", my_team=None, enemy_team=None):
        """生成测试用 session"""
        session = {
            "phase": phase,
            "localPlayerCellId": 0,
            "timer": {
                "phase": phase,
                "adjustedTimeLeftInPhase": 30000,
                "totalTimeInPhase": 30000,
            },
            "myTeam": my_team or [],
            "enemyTeam": enemy_team or [],
        }
        return session

    def test_no_session_404(self):
        """无 Champ Select → None → NO_SESSION"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._champ_session = None

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 0  # 第一次无 session 不发布

    def test_first_active_session(self):
        """首次 ACTIVE_SESSION"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session()

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "STARTED"

    def test_same_session_no_duplicate(self):
        """相同 session 不重复事件"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 1, "isPicked": False}]
        )

        self.runtime._poll_lcu()  # 第一次
        self.events_received.clear()

        self.runtime._poll_lcu()  # 第二次，相同

        events = self._get_champ_events()
        assert len(events) == 0

    def test_session_update(self):
        """session 内容变化 → UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 0, "isPicked": False}]
        )

        self.runtime._poll_lcu()
        self.events_received.clear()

        # 玩家选了英雄
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"

    def test_active_to_no_session(self):
        """ACTIVE → NO_SESSION → ENDED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session()

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # 离开 Champ Select
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._champ_session = None

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "ENDED"

    def test_reconnect_recovers(self):
        """reconnect 后恢复"""
        # 断开
        self.mock_lcu.connected = False
        self.mock_lcu._champ_session = None
        self.runtime._poll_lcu()
        self.events_received.clear()

        # 重连 + 进入 Champ Select
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session()

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "STARTED"

    def test_http_exception_no_crash(self):
        """HTTP exception 不 crash"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu.get_champ_select_session = MagicMock(side_effect=Exception("HTTP 500"))

        self.runtime._poll_lcu()  # 不应该崩溃

    def test_invalid_json_no_crash(self):
        """invalid JSON / 结构异常 不 crash"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = "not a dict"  # 无效结构

        self.runtime._poll_lcu()  # 不应该崩溃

    def test_missing_optional_fields(self):
        """缺少可选字段不崩溃"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        # 只有基本字段
        self.mock_lcu._champ_session = {
            "phase": "Pick",
            "localPlayerCellId": 0,
        }

        self.runtime._poll_lcu()  # 不应该崩溃

        events = self._get_champ_events()
        assert len(events) == 1

    def test_normalized_state_correct(self):
        """normalized snapshot 正确"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            phase="Pick",
            my_team=[
                {"cellId": 0, "championId": 64, "isPicked": True, "isBanned": False},
                {"cellId": 1, "championId": 0, "isPicked": False, "isBanned": False},
            ],
            enemy_team=[
                {"championId": -1, "isBanned": True},
            ]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        session = events[0].data["session"]
        assert session["status"] == "ACTIVE_SESSION"
        assert session["phase"] == "Pick"
        assert session["timer"]["phase"] == "Pick"
        assert len(session["my_team_champions"]) == 1  # 只有选了英雄的
        assert len(session["enemy_team_champions"]) == 1

    def test_state_store_integration(self):
        """StateStore integration"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session()

        self.runtime._poll_lcu()

        stored = self.runtime.state_store.get("lcu", "champ_select")
        assert stored is not None
        assert stored["status"] == "ACTIVE_SESSION"

    def test_eventbus_integration(self):
        """EventBus integration"""
        received = []

        def subscriber(event: Event):
            received.append(event)

        self.event_bus.subscribe(EventType.LCU_CHAMP_SELECT_CHANGED, subscriber)

        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session()

        self.runtime._poll_lcu()

        assert len(received) == 1
        assert received[0].data["status"] == "STARTED"

    def test_tencent_style_payload(self):
        """Tencent 风格真实 fixture"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        # 模拟 Tencent LCU 可能返回的结构
        self.mock_lcu._champ_session = {
            "httpStatus": 200,
            "phase": "Ban",
            "localPlayerCellId": 2,
            "localPlayerPosition": "middle",
            "localPlayerTeam": 1,
            "timer": {
                "phase": "Ban",
                "isInfinite": False,
                "adjustedTimeLeftInPhase": 25000,
                "totalTimeInPhase": 30000,
            },
            "myTeam": [
                {
                    "cellId": 0,
                    "championId": 0,
                    "championPickIntent": 0,
                    "isPicked": False,
                    "isBanned": False,
                },
                {
                    "cellId": 1,
                    "championId": 0,
                    "championPickIntent": 0,
                    "isPicked": False,
                    "isBanned": False,
                },
                {
                    "cellId": 2,
                    "championId": 0,
                    "championPickIntent": 0,
                    "isPicked": False,
                    "isBanned": False,
                },
            ],
            "enemyTeam": [
                {
                    "championId": 0,
                    "isBanned": False,
                },
            ],
            "actions": [],
            "bans": {
                "myTeamBans": [],
                "theirTeamBans": [],
            },
        }

        self.runtime._poll_lcu()  # 不应该崩溃

        events = self._get_champ_events()
        assert len(events) == 1
        session = events[0].data["session"]
        assert session["phase"] == "Ban"
        assert session["timer"]["phase"] == "Ban"

    def test_no_spam_on_timer_change(self):
        """timer 变化不产生事件（避免每秒 spam）"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            phase="Pick",
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # timer 时间变化（但 phase 和选人不变）
        self.mock_lcu._champ_session = self._make_session(
            phase="Pick",
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )
        self.mock_lcu._champ_session["timer"]["adjustedTimeLeftInPhase"] = 20000

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 0  # 不应该 spam

    # ===== 返工新增测试：字段级变化检测 =====

    def test_champion_id_change_triggers_update(self):
        """Test A：championId 变化 → UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 0, "isPicked": False}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # championId 从 0 变成 157
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 157, "isPicked": False}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"

    def test_champion_pick_intent_change_triggers_update(self):
        """Test B：championPickIntent 变化 → UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 0, "championPickIntent": 0, "isPicked": False}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # pick intent 从 0 变成 157
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 0, "championPickIntent": 157, "isPicked": False}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"

    def test_is_picked_change_triggers_update(self):
        """Test C：isPicked False → True → UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": False}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # 锁定选择
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"

    def test_is_banned_change_triggers_update(self):
        """Test D：isBanned False → True → UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            enemy_team=[{"championId": 157, "isBanned": False}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # 被 ban 了
        self.mock_lcu._champ_session = self._make_session(
            enemy_team=[{"championId": 157, "isBanned": True}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"

    def test_local_player_cell_id_change(self):
        """Test E：local player cellId 变化 → UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session()
        self.mock_lcu._champ_session["localPlayerCellId"] = 0

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # cellId 变化
        self.mock_lcu._champ_session = self._make_session()
        self.mock_lcu._champ_session["localPlayerCellId"] = 2

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"

    def test_exact_same_state_no_event(self):
        """Test F：完全相同状态不重复事件"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            phase="Pick",
            my_team=[
                {"cellId": 0, "championId": 64, "championPickIntent": 0, "isPicked": True, "isBanned": False},
                {"cellId": 1, "championId": 55, "championPickIntent": 0, "isPicked": True, "isBanned": False},
            ],
            enemy_team=[{"championId": 157, "isBanned": True}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # 完全相同的 session
        self.mock_lcu._champ_session = self._make_session(
            phase="Pick",
            my_team=[
                {"cellId": 0, "championId": 64, "championPickIntent": 0, "isPicked": True, "isBanned": False},
                {"cellId": 1, "championId": 55, "championPickIntent": 0, "isPicked": True, "isBanned": False},
            ],
            enemy_team=[{"championId": 157, "isBanned": True}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 0  # 完全相同不重复

    def test_irrelevant_raw_field_change_no_event(self):
        """Test G：raw 无关字段变化不触发 UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # 增加一个 normalized state 不关心的字段
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )
        self.mock_lcu._champ_session["someInternalField"] = "random_value"
        self.mock_lcu._champ_session["anotherField"] = {"nested": "data"}

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 0  # 无关字段不触发

    def test_timer_countdown_change_no_spam(self):
        """timer 倒计时变化不 spam（100 → 99）"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # 倒计时变化（100000 → 99000），其他完全相同
        self.mock_lcu._champ_session = self._make_session(
            my_team=[{"cellId": 0, "championId": 64, "isPicked": True}]
        )
        self.mock_lcu._champ_session["timer"]["adjustedTimeLeftInPhase"] = 99000

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 0  # 倒计时变化不 spam

    def test_timer_phase_change_triggers_update(self):
        """timer.phase 变化（Ban → Pick）触发 UPDATED"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "ChampSelect"
        self.mock_lcu._champ_session = self._make_session(
            phase="Ban",
            my_team=[{"cellId": 0, "championId": 0, "isPicked": False}]
        )

        self.runtime._poll_lcu()  # STARTED
        self.events_received.clear()

        # timer.phase 从 Ban 变成 Pick
        self.mock_lcu._champ_session = self._make_session(
            phase="Pick",
            my_team=[{"cellId": 0, "championId": 0, "isPicked": False}]
        )

        self.runtime._poll_lcu()

        events = self._get_champ_events()
        assert len(events) == 1
        assert events[0].data["status"] == "UPDATED"
