"""
GAME-REAL-06: game_time 停滞检测 phase 守卫测试
（训练营 queue 890 误判回归：8ed7721 双门槛 + 实时 phase 修正）
"""
import pytest

pytestmark = pytest.mark.unit

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.core.event_bus import EventType


class TestRuntimeStagnantPhaseGuard:
    """停滞检测：实时 phase 守卫"""

    def _make_runtime(self, lcu_get_return):
        from app.lol.runtime import LoLRuntime

        config = {
            "live_client": {"base_url": "https://127.0.0.1:2999", "poll_interval": 1.0},
            "lcu": {"enabled": False},
            "game_alarm": {},
        }

        with patch("app.lol.runtime.LiveClientAPI"), \
             patch("app.lol.runtime.LCUConnection"), \
             patch("app.lol.runtime.StateDiff"), \
             patch("app.lol.runtime.GameAlarmEngine"), \
             patch("app.lol.runtime.get_event_bus") as mock_get_bus, \
             patch("app.lol.runtime.get_scheduler") as mock_get_scheduler, \
             patch("app.lol.runtime.get_state_store") as mock_get_store:

            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            mock_scheduler = MagicMock()
            mock_get_scheduler.return_value = mock_scheduler
            mock_store = MagicMock()
            mock_get_store.return_value = mock_store

            runtime = LoLRuntime(config)
            runtime.event_bus = mock_bus
            runtime.scheduler = mock_scheduler
            runtime.state_store = mock_store

        runtime._running = True
        # 控制 L191-192 / L153-154 实时 phase（改动后读 self.lcu.get）
        runtime.lcu.get.return_value = lcu_get_return

        # 停滞临界状态：停滞已累积 max-1 = 9 轮，本轮再停滞即触发守卫
        runtime._game_active = True
        runtime._game_ended_waiting_new_game = False
        runtime._last_game_time = 1.0
        runtime._game_time_stagnant_count = runtime._max_stagnant_count - 1
        runtime._game_time_stagnant_fired = False
        runtime._game_time_ever_progressed = True
        # 旧缓存停在非集合（训练营绕过场景：_poll_lcu L343 直接 return）
        runtime._last_known_phase = "ChampSelect"
        runtime._game_end_published = False
        runtime._game_end_retry_count = 0
        runtime._cached_game_end_result = None
        return runtime, mock_bus

    @contextmanager
    def _snapshot_context(self, runtime, game_time=1.0):
        """SnapshotBuilder patch 上下文：_poll_game_data 调用必须在 with 内执行
        （with 块外 patch 已卸载，真实 SnapshotBuilder 会把 {"Events": []} 解析出 game_time=0，
         走 current_gt <= 0 分支不进停滞检测，用例会静默失效）"""
        with patch("app.lol.runtime.SnapshotBuilder") as mock_sb:
            snap = MagicMock()
            snap.game_time = game_time
            mock_sb.from_api_data.return_value = snap
            runtime.live_api.is_available.return_value = True
            runtime.live_api.get_all_game_data.return_value = {"Events": []}
            yield mock_sb

    def test_stagnant_phase_in_progress_no_game_end(self):
        """① 停滞 + 实时 phase=InProgress → 不判结束（旧缓存 ChampSelect 被绕过场景）"""
        runtime, mock_bus = self._make_runtime(lcu_get_return="InProgress")
        with self._snapshot_context(runtime):
            runtime._poll_game_data()
        assert runtime._game_end_retry_count == 0
        assert runtime._game_end_published is False
        assert not any(
            c[0][0].event_type == EventType.GAME_ENDED
            for c in mock_bus.publish.call_args_list
        )

    def test_stagnant_lcu_channel_down_no_game_end(self):
        """② 停滞 + LCU 通道异常（lcu.get 返回 None）→ 不判结束（训练营场景）"""
        runtime, mock_bus = self._make_runtime(lcu_get_return=None)
        with self._snapshot_context(runtime):
            runtime._poll_game_data()
        assert runtime._game_end_retry_count == 0
        assert runtime._game_end_published is False

    def test_stagnant_phase_lobby_game_end(self):
        """③ 停滞 + 真离开游戏（phase=Lobby + ever_progressed=True）→ 进入检测"""
        runtime, mock_bus = self._make_runtime(lcu_get_return="Lobby")
        with self._snapshot_context(runtime):
            runtime._poll_game_data()
        assert runtime._game_end_retry_count == 1  # grace window 第一次重试
        assert runtime._game_end_published is False  # 未到最终发布（max retries=2）

    def test_available_fail_phase_lobby_game_end(self):
        """④ is_available 失败 + 实时 phase=Lobby → 进入检测（原行为保留）"""
        runtime, mock_bus = self._make_runtime(lcu_get_return="Lobby")
        runtime.live_api.is_available.return_value = False
        runtime._poll_game_data()
        assert runtime._game_end_retry_count == 1
