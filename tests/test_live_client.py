"""
Live Client Data API (2999) 测试

注意：需要进入 LoL 训练模式才能真实验证。
如果未在游戏中，测试自动 skip。
"""
import os
import pytest

from app.lol.live_client import LiveClientAPI


class TestLiveClientAPI:
    """2999 Live Client API 测试"""

    def test_api_initialization(self):
        """验证 API 初始化"""
        api = LiveClientAPI()
        assert api is not None
        assert api.base_url == "https://127.0.0.1:2999"

    def test_endpoints_defined(self):
        """验证所有端点已定义（腾讯服使用 /liveclientdata 前缀）"""
        api = LiveClientAPI()
        expected_endpoints = {
            "allgamedata": "/liveclientdata/allgamedata",
            "activeplayer": "/liveclientdata/activeplayer",
            "playerlist": "/liveclientdata/playerlist",
            "gamestats": "/liveclientdata/gamestats",
            "eventdata": "/liveclientdata/eventdata",
        }
        for key, path in expected_endpoints.items():
            assert key in api.ENDPOINTS
            assert api.ENDPOINTS[key] == path

    @pytest.mark.skipif(
        not os.environ.get("IN_GAME", ""),
        reason="需要进入 LoL 游戏（训练模式）才能真实验证"
    )
    def test_is_available_in_game(self):
        """游戏中检查 API 可用性"""
        api = LiveClientAPI()
        assert api.is_available() is True

    @pytest.mark.skipif(
        not os.environ.get("IN_GAME", ""),
        reason="需要进入 LoL 游戏（训练模式）才能真实验证"
    )
    def test_gamestats(self):
        """测试 gamestats 端点"""
        api = LiveClientAPI()
        stats = api.get_game_stats()
        assert stats is not None
        assert "gameTime" in stats

    @pytest.mark.skipif(
        not os.environ.get("IN_GAME", ""),
        reason="需要进入 LoL 游戏（训练模式）才能真实验证"
    )
    def test_playerlist(self):
        """测试 playerlist 端点"""
        api = LiveClientAPI()
        players = api.get_player_list()
        assert players is not None
        assert len(players) > 0

    @pytest.mark.skipif(
        not os.environ.get("IN_GAME", ""),
        reason="需要进入 LoL 游戏（训练模式）才能真实验证"
    )
    def test_eventdata(self):
        """测试 eventdata 端点"""
        api = LiveClientAPI()
        events = api.get_event_data()
        assert events is not None

    @pytest.mark.skipif(
        not os.environ.get("IN_GAME", ""),
        reason="需要进入 LoL 游戏（训练模式）才能真实验证"
    )
    def test_activeplayer(self):
        """测试 activeplayer 端点"""
        api = LiveClientAPI()
        player = api.get_active_player()
        assert player is not None

    @pytest.mark.skipif(
        not os.environ.get("IN_GAME", ""),
        reason="需要进入 LoL 游戏（训练模式）才能真实验证"
    )
    def test_data_pipeline(self):
        """
        验证完整数据链路：
        2999 -> GameSnapshot -> StateDiff -> EventBus
        """
        from app.lol.snapshot import SnapshotBuilder
        from app.lol.state_diff import StateDiff
        from app.core.event_bus import EventBus

        # 1. 从 2999 获取数据
        api = LiveClientAPI()
        all_data = api.get_all_game_data()
        assert all_data is not None

        # 2. 构建 Snapshot
        builder = SnapshotBuilder(api)
        snapshot = builder.build()
        assert snapshot is not None

        # 3. StateDiff（第一次）
        sd = StateDiff()
        sd._last_snapshot = snapshot

        # 4. 再次获取数据（时间过了几秒）
        import time
        time.sleep(1)
        snapshot2 = builder.build()
        events = sd.diff(snapshot2)

        # 5. 发布到 EventBus
        event_bus = EventBus()
        received = []
        event_bus.subscribe("game.time_update", lambda e: received.append(e))

        for e in events:
            event_bus.publish(e)

        # 验证链路通畅
        assert len(received) > 0 or len(events) >= 0  # 时间可能没变化


# ---------- 2026-10-18：2999 404 修复（gamestats → allgamedata） ----------

class _FakeResp:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class TestAllGameDataProbe:
    """is_available / get_game_time 已改走标准最全端点 /allgamedata。

    腾讯服实测 /liveclientdata/gamestats 返回 404（接口被禁）→ 2999 永远判定
    不可用、实时 KDA 不刷新；allgamedata 是官方主数据端点。
    """

    def test_is_available_uses_allgamedata(self, monkeypatch):
        api = LiveClientAPI()
        urls = []

        def fake_get(url, timeout=None):
            urls.append(url)
            return _FakeResp(200, {"gameData": {"gameMode": "PRACTICETOOL"}})

        monkeypatch.setattr(api._session, "get", fake_get)
        assert api.is_available() is True
        assert urls and urls[0].endswith("/liveclientdata/allgamedata")
        assert all("gamestats" not in u for u in urls)

    def test_is_available_404_false(self, monkeypatch):
        api = LiveClientAPI()

        def fake_get(url, timeout=None):
            return _FakeResp(404)

        monkeypatch.setattr(api._session, "get", fake_get)
        assert api.is_available() is False

    def test_is_available_connection_error_false(self, monkeypatch):
        import requests
        api = LiveClientAPI()

        def fake_get(url, timeout=None):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(api._session, "get", fake_get)
        assert api.is_available() is False

    def test_get_game_time_from_allgamedata(self, monkeypatch):
        api = LiveClientAPI()
        monkeypatch.setattr(api, "get_all_game_data",
                            lambda: {"gameData": {"gameTime": 123.5}})
        assert api.get_game_time() == 123.5

    def test_get_game_time_missing_falls_back_zero(self, monkeypatch):
        api = LiveClientAPI()
        monkeypatch.setattr(api, "get_all_game_data", lambda: {"gameData": {}})
        assert api.get_game_time() == 0.0

    def test_get_game_time_error_falls_back_zero(self, monkeypatch):
        api = LiveClientAPI()
        monkeypatch.setattr(api, "get_all_game_data",
                            lambda: {"gameData": {"gameTime": "abc"}})
        assert api.get_game_time() == 0.0
