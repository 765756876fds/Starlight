"""
runtime GAME_ENDED 判定守卫（新修复版）单元测试

覆盖 6 个场景（对应修 1/修 2/修 3/修 4 + A 方案）：
a) 加载界面 game_time>0 停滞 + phase=GameStart（在集合）→ 不发布
b) phase=None（LCU 从未连上）+ game_time 停滞 → 不发布
c) game_time 从未递增（ever_progressed=False）+ 停滞 + phase 离开 → 不发布
d) 真结束（phase 离开 + 曾递增 + 停滞）→ 发布一次且不循环
e) 6607e1a 兜底（2999 挂着 + phase 离开 + count 累积）→ 发布一次
f) state_diff 路径（A 方案）→ 发布一次 + _game_active=False
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.event_bus import EventType
from app.lol.events import GameEvent, GameEventType
from app.lol.runtime import LoLRuntime


def make_snap(game_time: float) -> dict:
    """构造 2999 all_data 最小结构（空玩家列表，SnapshotBuilder 可安全解析）"""
    return {
        "gameData": {
            "gameTime": game_time,
            "gameMode": "CLASSIC",
            "mapName": "Summoner's Rift",
            "mapNumber": 11,
        },
        "allPlayers": [],
        "activePlayer": {},
        "events": {"Events": []},
    }


class GameEndGuardFixTest(unittest.TestCase):

    def setUp(self):
        with patch("app.lol.runtime.PlayerRepository"), \
             patch("app.lol.runtime.PlayerAnalysisService"), \
             patch("app.lol.runtime.LiveClientAPI"), \
             patch("app.lol.runtime.LCUConnection"), \
             patch("app.lol.runtime.GameAlarmEngine"), \
             patch("app.lol.runtime.ObjectiveTimer"), \
             patch("app.lol.runtime.get_event_bus"), \
             patch("app.lol.runtime.get_scheduler"), \
             patch("app.lol.runtime.get_state_store"):
            self.rt = LoLRuntime({
                "live_client": {"enabled": True, "poll_interval": 1.0},
                "lcu": {"enabled": True},
                "game_alarm": {},
            })
        # 可控子系统（不跑真实 scheduler / 网络 / 事件总线）
        self.rt._running = True  # 必须置位，否则 _poll_game_data 第一行直接 return
        self.rt.live_api = MagicMock()
        self.rt.live_api.is_available.return_value = True
        self.rt.live_api.get_all_game_data.return_value = make_snap(0.0)
        self.rt.live_api.get_event_data.return_value = {"Events": []}
        self.rt.state_diff = MagicMock()
        self.rt.state_diff.diff.return_value = []
        self.rt.player_analysis = MagicMock()
        self.rt.event_bus = MagicMock()

    # ---- 辅助 ----

    def _polls(self, n, game_time):
        """连续 n 轮轮询，game_time 固定（停滞）"""
        self.rt.live_api.get_all_game_data.return_value = make_snap(game_time)
        for _ in range(n):
            self.rt._poll_game_data()

    def _progress(self, times):
        """game_time 递增若干轮（触发 ever_progressed=True）"""
        for t in times:
            self.rt.live_api.get_all_game_data.return_value = make_snap(t)
            self.rt._poll_game_data()

    def _game_end_count(self):
        calls = self.rt.event_bus.publish.call_args_list
        return sum(1 for c in calls if c.args[0].event_type == EventType.GAME_ENDED)

    # ---- 场景 ----

    def test_a_loading_phase_in_game_not_publish(self):
        """a) 加载界面 game_time>0 停滞 + phase=GameStart（在集合）→ 不发布"""
        self.rt._last_known_phase = "GameStart"
        self.rt._game_active = True
        self._polls(12, game_time=5.0)
        self.assertEqual(self._game_end_count(), 0)
        # 第 1 轮 5.0 != -1.0（初始值）→ count 重置为 0；第 2-12 轮累加到 11
        self.assertEqual(self.rt._game_time_stagnant_count, 11)

    def test_b_phase_none_not_publish(self):
        """b) phase=None（LCU 从未连上）+ 停滞 → 不发布（None 视为仍在游戏）"""
        self.rt._last_known_phase = None
        self.rt._game_active = True
        self._polls(12, game_time=5.0)
        self.assertEqual(self._game_end_count(), 0)

    def test_c_never_progressed_not_publish(self):
        """c) ever_progressed=False + 停滞 + phase 离开 → 不发布（修 2 门槛）"""
        self.rt._last_known_phase = "EndOfGame"  # 不在集合
        self.rt._game_active = True
        self.rt._game_time_ever_progressed = False
        self._polls(12, game_time=5.0)
        self.assertEqual(self._game_end_count(), 0)

    def test_d_real_end_publish_once_no_loop(self):
        """d) 真结束：phase 离开 + 曾递增 + 停滞 → 发布一次且不循环"""
        self.rt._last_known_phase = "InProgress"
        self.rt._game_active = True
        self._progress([1.0, 2.0, 3.0, 4.0, 5.0])  # ever_progressed=True
        self.assertTrue(self.rt._game_time_ever_progressed)
        # phase 离开集合 → waiting=True（模拟 _poll_lcu 集合→非集合边沿）
        self.rt._last_known_phase = "EndOfGame"
        self.rt._game_ended_waiting_new_game = True
        self._polls(15, game_time=5.0)  # 10 轮后触发（含 2 轮 grace），再 5 轮确认不循环
        self.assertEqual(self._game_end_count(), 1)

    def test_e_2999_hanging_publish_once(self):
        """e) 6607e1a 兜底：2999 挂着 + phase 离开 + count 累积 → 发布一次"""
        # 上局曾递增；phase 离开时 ever_progressed 保留（修 4：离开不清零）
        self.rt._last_known_phase = "EndOfGame"
        self.rt._game_active = True
        self.rt._game_time_ever_progressed = True
        self.rt._game_ended_waiting_new_game = True
        self._polls(15, game_time=9.0)
        self.assertEqual(self._game_end_count(), 1)

    def test_f_state_diff_path_sets_active_false(self):
        """f) state_diff 路径（A 方案）→ 发布一次 + _game_active=False"""
        self.rt._last_known_phase = "InProgress"
        self.rt._game_active = True
        self._progress([1.0, 2.0, 3.0])  # game_time 递增，不触发停滞
        self.rt.state_diff.diff.return_value = [GameEvent(
            event_type=GameEventType.GAME_ENDED,
            game_time=3.0,
            data={},
            timestamp=0.0,
        )]
        self.rt._poll_game_data()
        self.assertEqual(self._game_end_count(), 1)
        self.assertFalse(self.rt._game_active)


if __name__ == "__main__":
    unittest.main()
