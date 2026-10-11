"""
runtime 游戏结束误判守卫测试：
- gameTime <= 0（加载界面，游戏未正式开始）不算停滞，不触发游戏结束检测
- gameTime 停滞（真结束场景）仍能触发游戏结束检测（防改过头）
- is_available=False 时 phase 守卫拦住加载/对局中误判（2999 暂不可用不算结束）
- is_available=False 时 phase 已离开游戏则放行真结束检测
"""
from unittest.mock import MagicMock, patch


def _make_all_data(game_time: float) -> dict:
    return {
        "gameData": {
            "gameTime": game_time,
            "gameMode": "CLASSIC",
            "mapName": "",
            "mapNumber": 0,
        },
        "allPlayers": [],
        "activePlayer": {},
        "events": {"Events": []},
    }


class TestRuntimeGameEndGuard:
    def _create_runtime(self):
        """创建 LoLRuntime（mock 外部依赖，可完整跑 _poll_game_data）"""
        from app.lol.runtime import LoLRuntime

        config = {
            "live_client": {
                "base_url": "https://127.0.0.1:2999",
                "poll_interval": 1.0,
            },
            "lcu": {"enabled": False},
            "game_alarm": {},
        }

        with patch("app.lol.runtime.LiveClientAPI"), \
             patch("app.lol.runtime.LCUConnection"), \
             patch("app.lol.runtime.StateDiff"), \
             patch("app.lol.runtime.GameAlarmEngine"), \
             patch("app.lol.runtime.PlayerAnalysisService"), \
             patch("app.lol.runtime.PlayerRepository"), \
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

        # 完整跑 _poll_game_data 主流程所需的 mock
        runtime._running = True
        runtime.player_analysis = MagicMock()
        runtime.state_diff.diff.return_value = []
        runtime.live_api.get_event_data.return_value = {"Events": []}
        runtime._handle_game_end_detection = MagicMock()
        return runtime

    def test_game_time_zero_never_triggers_stagnant(self):
        """加载界面 gameTime=0：连续 12 帧不算停滞，不触发游戏结束"""
        runtime = self._create_runtime()
        runtime._game_active = True
        runtime.live_api.is_available.return_value = True
        runtime.live_api.get_all_game_data.return_value = _make_all_data(0.0)

        for _ in range(12):
            runtime._poll_game_data()

        runtime._handle_game_end_detection.assert_not_called()

    def test_game_time_stagnant_still_triggers(self):
        """真结束场景 game_time=100 连续 11 帧不变：仍触发游戏结束检测（防改过头）"""
        runtime = self._create_runtime()
        runtime._game_active = True
        runtime.live_api.is_available.return_value = True
        runtime.live_api.get_all_game_data.return_value = _make_all_data(100.0)

        for _ in range(11):
            runtime._poll_game_data()

        runtime._handle_game_end_detection.assert_called()

    def test_guard_blocks_when_phase_in_game(self):
        """is_available=False 但 phase=InProgress（加载/对局中）：不判结束"""
        runtime = self._create_runtime()
        runtime._game_active = True
        runtime._last_known_phase = "InProgress"
        runtime.live_api.is_available.return_value = False

        runtime._poll_game_data()

        runtime._handle_game_end_detection.assert_not_called()

    def test_guard_blocks_unknown_phase(self):
        """is_available=False 且 phase=Unknown（LCU 未识别）：按仍在游戏中处理，不判结束"""
        runtime = self._create_runtime()
        runtime._game_active = True
        runtime._last_known_phase = "Unknown"
        runtime.live_api.is_available.return_value = False

        runtime._poll_game_data()

        runtime._handle_game_end_detection.assert_not_called()

    def test_guard_passes_when_phase_left_game(self):
        """is_available=False 且 phase=EndOfGame（真结束）：放行结束检测"""
        runtime = self._create_runtime()
        runtime._game_active = True
        runtime._last_known_phase = "EndOfGame"
        runtime.live_api.is_available.return_value = False

        runtime._poll_game_data()

        runtime._handle_game_end_detection.assert_called_once()

    def test_last_known_phase_updated_on_poll_lcu(self):
        """赋值链路：_poll_lcu 拿到有效 phase 时更新 _last_known_phase"""
        runtime = self._create_runtime()
        runtime.lcu.connected = True
        runtime.lcu.get_gameflow_phase.return_value = "InProgress"

        runtime._poll_lcu()

        assert runtime._last_known_phase == "InProgress"

    def test_last_known_phase_kept_on_lcu_disconnect(self):
        """断连保留旧值：LCU 断连时 _last_known_phase 不被清 None"""
        runtime = self._create_runtime()
        runtime._last_known_phase = "InProgress"
        runtime.lcu.connected = False
        runtime._lcu_was_connected = True  # 触发断连清理分支

        runtime._poll_lcu()

        assert runtime._last_known_phase == "InProgress"
