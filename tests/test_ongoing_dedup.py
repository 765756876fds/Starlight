"""Ongoing 服务 P0a 断连熔断 + P0b session 防抖测试。

覆盖场景：
- 连接断开时跳过 tick、不清空 session（P0a）
- 同 game_id 只发一次 session_created（P0b）
- history retry 直接 force 重载、不重发 session_created（P0b）
- 腾讯服 gameId 复用：离开对局清空后重新宣布（P0b 关键补充）
"""
import time

from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.match_model import MatchHistoryResult, ERROR


def _gf_cell(puuid, sid=1):
    return {
        "puuid": puuid, "summonerId": sid, "cellId": 1,
        "riotIdGameName": puuid, "riotId": puuid,
        "championId": 1, "assignedPosition": "",
    }


class FakeLCU:
    """_connected/_session 可控；base_url/_auth_token 留空 → dget 走退化共享路径。"""

    def __init__(self, connected=True):
        self._connected = connected
        self._session = object() if connected else None
        self.base_url = ""
        self._auth_token = ""
        self._responses = {
            "/lol-summoner/v1/current-summoner": {"puuid": "me"},
        }

    def get(self, ep):
        if ep in self._responses:
            return self._responses[ep]
        if ep.startswith("/lol-summoner/v2/summoners/by-puuid/"):
            return {"summonerId": 7, "displayName": "x", "summonerLevel": 30}
        return None

    def request_raw(self, method, ep, data=None):
        return (200, self.get(ep))

    def set_phase(self, phase):
        self._responses["/lol-gameflow/v1/gameflow-phase"] = phase

    def set_gameflow(self, game_id):
        self._responses["/lol-gameflow/v1/session"] = {
            "gameId": game_id,
            "gameData": {
                "teamOne": [_gf_cell("me")] + [_gf_cell(f"t1-{i}") for i in range(4)],
                "teamTwo": [_gf_cell(f"t2-{i}") for i in range(5)],
            },
        }


class FakeAcct:
    def recent(self, puuid, count=None):
        return MatchHistoryResult(state=ERROR, message="lcu_unavailable")


def _make_svc(lcu, events):
    return OngoingGameService(
        lcu=lcu,
        on_event=lambda c, d: events.append((c, d)),
        account_history_service=FakeAcct(),
        snapshot_provider=lambda: None,
        gsm_provider=None,
    )


def _codes(events):
    return [c for c, _ in events]


def test_tick_skips_when_lcu_disconnected():
    """P0a：连接断开时跳过 tick，不清空已有 session"""
    lcu = FakeLCU(connected=False)
    events = []
    svc = _make_svc(lcu, events)
    sentinel = object()
    svc._session = sentinel  # 模拟已有 session
    svc._current_session_key = "gid:123"

    svc._tick()

    assert "ongoing.conn_skip" in _codes(events)
    assert svc._session is sentinel          # 不清空
    assert svc._current_session_key == "gid:123"
    assert "ongoing.session_created" not in _codes(events)


def test_phase_none_with_disconnect_skips():
    """P0a：phase 直连失败且共享连接断开 → 跳过本轮（不清空）"""
    lcu = FakeLCU(connected=False)
    lcu.set_phase("InProgress")  # 但 _connected=False → 熔断优先
    events = []
    svc = _make_svc(lcu, events)
    sentinel = object()
    svc._session = sentinel

    svc._tick()

    assert "ongoing.conn_skip" in _codes(events)
    assert svc._session is sentinel


def test_session_created_once_per_game_id():
    """P0b：同 game_id 两次 tick → session_created 只发 1 次"""
    lcu = FakeLCU(connected=True)
    lcu.set_phase("InProgress")
    lcu.set_gameflow(123)
    events = []
    svc = _make_svc(lcu, events)

    svc._tick()
    time.sleep(0.3)   # 等加载线程收尾
    svc._tick()
    time.sleep(0.3)

    created = [d for c, d in events if c == "ongoing.session_created"]
    assert len(created) == 1
    assert created[0]["game_id"] == 123


def test_retry_does_not_reemit_session_created():
    """P0b：retry（missing>0 且到间隔）直接 force 重载，不重发 session_created"""
    lcu = FakeLCU(connected=True)
    lcu.set_phase("InProgress")
    lcu.set_gameflow(123)
    events = []
    svc = _make_svc(lcu, events)

    svc._tick()
    time.sleep(0.3)   # 首次加载完成 → ERROR 快照入缓存
    assert "ongoing.history_summary" in _codes(events)
    first_created = [d for c, d in events if c == "ongoing.session_created"]
    assert len(first_created) == 1
    req_before = sum(1 for c, _ in events if c == "history.request")

    # 把 retry 状态拨到"已过 60s 间隔"以触发 retry（离线可控）
    svc._retry_state["gid:123"] = [0, time.monotonic() - 61.0]
    svc._tick()
    time.sleep(0.3)   # 等 retry 加载线程

    created_after = [d for c, d in events if c == "ongoing.session_created"]
    req_after = sum(1 for c, _ in events if c == "history.request")
    assert len(created_after) == 1           # 不重发 session_created
    assert req_after > req_before            # 但确实重新请求了（force 重载）
    assert "ongoing.history_summary" in _codes(events)


def test_game_id_reused_after_leave_reannounced():
    """P0b 关键：腾讯服 gameId 复用——离开对局清空后，同 gameId 新局重新宣布"""
    lcu = FakeLCU(connected=True)
    lcu.set_phase("InProgress")
    lcu.set_gameflow(123)
    events = []
    svc = _make_svc(lcu, events)

    svc._tick()                              # 第一局：宣布
    time.sleep(0.3)
    assert sum(1 for c, _ in events if c == "ongoing.session_created") == 1

    lcu.set_phase("Lobby")                   # 对局结束
    svc._tick()
    assert svc._session is None
    assert svc._announced_session_id is None # 防抖已清空
    assert svc._retry_state == {}            # retry 计数已清空

    lcu.set_phase("InProgress")              # 第二局：复用 gameId=123
    lcu.set_gameflow(123)
    svc._tick()
    time.sleep(0.3)

    assert sum(1 for c, _ in events if c == "ongoing.session_created") == 2


def test_gameflow_dump_only_on_change():
    """dump 日志只在 (gameMode/人数/puuid数) 变化时打：同局稳定不刷屏，变化时再打"""
    import builtins
    from unittest import mock

    lcu = FakeLCU(connected=True)
    lcu.set_phase("InProgress")
    lcu.set_gameflow(123)
    events = []
    svc = _make_svc(lcu, events)

    with mock.patch("builtins.print") as m:
        svc._tick()
        time.sleep(0.3)
        svc._tick()                              # 同局相同数据 → 不打
        time.sleep(0.3)
    dump_calls = [c for c in m.call_args_list if "gameflow session" in str(c)]
    assert len(dump_calls) == 1                  # 两次 tick 只打一次

    # 数据变化（teamTwo 人数变）→ 再打一次
    gf = lcu._responses["/lol-gameflow/v1/session"]
    gf["gameData"]["teamTwo"] = [_gf_cell("t2-x")]
    with mock.patch("builtins.print") as m2:
        svc._tick()
        time.sleep(0.3)
    dump_calls2 = [c for c in m2.call_args_list if "gameflow session" in str(c)]
    assert len(dump_calls2) == 1                  # 变化后打一次
