"""PHASE D OngoingGameLoader 测试（mock LCU）。"""
import pytest

from app.lol.ongoing.loader import OngoingGameLoader, RateLimiter
from app.lol.ongoing.cache import SnapshotCache


def _make_session(n=10):
    my_team = [{"puuid": f"puuid-m-{i}", "summonerId": 1000+i, "cellId": i,
                "assignedPosition": "MIDDLE"} for i in range(5)]
    their_team = [{"puuid": f"puuid-e-{i}", "summonerId": 2000+i, "cellId": i,
                   "assignedPosition": "BOTTOM"} for i in range(5)]
    return {"myTeam": my_team, "theirTeam": their_team}


class FakeLcu:
    def __init__(self):
        self.calls = []
        # P0a 断连熔断要求连接状态属性（service._tick 检查 lcu._connected/_session；
        # _session 须非空——空 dict 是 falsy，仍会触发 conn_skip）
        self._connected = True
        self._session = {"connected": True}
    def get(self, endpoint):
        self.calls.append(endpoint)
        if "/lol-summoner/v2/summoners/by-puuid/" in endpoint or "/lol-summoner/v1/summoners/by-puuid/" in endpoint:
            return {"displayName": "Player", "summonerLevel": 30,
                    "summonerId": 1, "puuid": "x"}
        if "/lol-summoner/v1/summoners/" in endpoint:
            return {"displayName": "Player", "summonerLevel": 30,
                    "summonerId": 1, "puuid": "x"}
        if "/lol-ranked/v1/ranked-stats/" in endpoint:
            return {"queues": [{"queueType": "RANKED_SOLO_5x5",
                                "tier": "GOLD", "division": "II", "leaguePoints": 20}]}
        if "/lol-match-history/v1/" in endpoint:
            return {"games": {"games": [
                {"championId": 157, "gameCreation": 1, "gameDuration": 1800,
                 "stats": {"win": True, "kills": 5, "deaths": 2, "assists": 8}}
            ]}}
        return None


class FakeAcct:
    """返回 AVAILABLE 1 条战绩的假战绩服务（匹配新缓存语义）。"""
    def __init__(self):
        self.calls = 0
    def recent(self, puuid, count=None):
        self.calls += 1
        from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE
        return MatchHistoryResult(state=AVAILABLE, records=[
            MatchRecord(game_id=1, game_creation=1, queue_id=420, champion_name="Yasuo",
                        win=True, kills=5, deaths=2, assists=8)
        ])


def test_extract_ten_players():
    l = OngoingGameLoader(lcu_get=lambda ep: None)
    players = l._extract_players(_make_session())
    assert len(players) == 10
    assert sum(1 for p in players if p["team"] == "myTeam") == 5
    assert sum(1 for p in players if p["team"] == "theirTeam") == 5


def test_load_returns_snapshots():
    fake = FakeLcu()
    l = OngoingGameLoader(lcu_get=fake.get, account_history_service=FakeAcct(),
                          max_workers=2, rps=100)
    snaps = l.load_for_champ_select(_make_session())
    assert len(snaps) == 10
    s = snaps[0]
    assert s.display_name == "Player"
    assert s.ranked_tier == "GOLD"
    assert len(s.recent_matches) == 1


def test_cache_hit_no_request():
    fake = FakeLcu()
    acct = FakeAcct()
    cache = SnapshotCache(ttl_seconds=300)
    l = OngoingGameLoader(lcu_get=fake.get, cache=cache, account_history_service=acct,
                          max_workers=2, rps=100)
    s1 = l.load_for_champ_select(_make_session())
    assert all(getattr(s, "data_state", "ERROR") == "AVAILABLE" for s in s1)
    n_calls_first = len(fake.calls)
    n_acct_first = acct.calls
    s2 = l.load_for_champ_select(_make_session())
    # 第二次全部命中 AVAILABLE 缓存，不应再请求（新语义：仅成功数据复用）
    assert len(fake.calls) == n_calls_first
    assert acct.calls == n_acct_first
    assert len(s2) == 10


def test_match_history_failure_isolation():
    class Flaky(FakeLcu):
        def get(self, endpoint):
            if "match-history" in endpoint:
                return None
            return super().get(endpoint)
    l = OngoingGameLoader(lcu_get=Flaky().get, max_workers=2, rps=100)
    snaps = l.load_for_champ_select(_make_session())
    assert len(snaps) == 10
    assert all(len(s.recent_matches) == 0 for s in snaps)


def test_rate_limiter():
    import time
    rl = RateLimiter(per_second=5)
    t0 = time.monotonic()
    for _ in range(5):
        rl.wait()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.6  # 5 req / 5 rps ≈ 0.8s


def test_clear_cache():
    fake = FakeLcu()
    l = OngoingGameLoader(lcu_get=fake.get, max_workers=2, rps=100)
    l.load_for_champ_select(_make_session())
    assert l._cache.size() == 10
    l.clear()
    assert l._cache.size() == 0


def test_summoner_fallback_v2_then_v1():
    """v2 by-puuid 返回 None → 走 v1 by-puuid。"""
    calls = []
    def get(ep):
        calls.append(ep)
        if "by-puuid" in ep and "v2" in ep:
            return None
        if "by-puuid" in ep and "v1" in ep:
            return {"displayName": "P", "summonerLevel": 30, "summonerId": 1}
        if "ranked-stats" in ep:
            return {"queues": []}
        if "match-history" in ep:
            return {"games": {"games": []}}
        return None
    l = OngoingGameLoader(lcu_get=get, max_workers=1, rps=100)
    snaps = l.load_for_champ_select(_make_session())
    assert len(snaps) == 10
    # 第一个调用必须是 v2，第二个是 v1
    assert any("v2/summoners/by-puuid" in c for c in calls)
    assert any("v1/summoners/by-puuid" in c for c in calls)


def test_ranked_not_self():
    """ranked 必须按 puuid 查，不能用 current-ranked-stats。"""
    calls = []
    def get(ep):
        calls.append(ep)
        if "by-puuid" in ep:
            return {"displayName": "P", "summonerLevel": 30, "summonerId": 1}
        if "ranked-stats/" in ep and "current" not in ep:
            return {"queues": [{"queueType": "RANKED_SOLO_5x5",
                                "tier": "SILVER", "division": "I", "leaguePoints": 5}]}
        if "match-history" in ep:
            return {"games": {"games": []}}
        return None
    l = OngoingGameLoader(lcu_get=get, max_workers=1, rps=100)
    snaps = l.load_for_champ_select(_make_session())
    assert all(s.ranked_tier == "SILVER" for s in snaps)
    assert not any("current-ranked-stats" in c for c in calls)


def test_service_independent_polling():
    """OngoingGameService 自己轮询 gameflow-phase，不依赖 automation 事件。"""
    from app.lol.ongoing.service import OngoingGameService
    phase_holder = {"phase": "Lobby"}

    def _gameflow_session():
        return {
            "gameId": 42,
            "gameData": {
                "teamOne": [{"puuid": f"puuid-m-{i}", "summonerId": 1000 + i, "cellId": i,
                             "championId": 1 + i, "riotIdGameName": f"M{i}"} for i in range(5)],
                "teamTwo": [{"puuid": f"puuid-e-{i}", "summonerId": 2000 + i, "cellId": i,
                             "championId": 20 + i, "riotIdGameName": f"E{i}"} for i in range(5)],
            },
        }

    def get(ep):
        if ep == "/lol-gameflow/v1/gameflow-phase":
            return phase_holder["phase"]
        if ep == "/lol-gameflow/v1/session":
            return _gameflow_session()
        if ep == "/lol-champ-select/v1/session":
            return _make_session()
        if ep == "/lol-summoner/v1/current-summoner":
            return {"puuid": "puuid-m-0"}
        if "by-puuid" in ep:
            return {"displayName": "P", "summonerLevel": 30, "summonerId": 1}
        if "ranked-stats" in ep:
            return {"queues": []}
        if "match-history" in ep:
            return {"games": {"games": []}}
        return None
    def request_raw(method, path, body=None, timeout=3.0):
        return None

    svc = OngoingGameService(lcu=type("L", (), {"get": staticmethod(get),
                                                "request_raw": staticmethod(request_raw),
                                                "_connected": True,
                                                "_session": {"connected": True}})(),
                              poll_interval=0.05, rps=100, max_workers=1)
    svc.start()
    import time
    time.sleep(0.3)
    assert svc.get_snapshots() == []
    # 对局阶段（ChampSelect/GameStart）gameflow 就绪即构建 10 人；此处用 GameStart 验证轮询链路
    phase_holder["phase"] = "GameStart"
    time.sleep(0.5)
    snaps = svc.get_snapshots()
    assert len(snaps) == 10
    # 同一 session 不重复
    n_load1 = len(snaps)
    time.sleep(0.3)
    assert len(svc.get_snapshots()) == n_load1
    # 切走清掉
    phase_holder["phase"] = "Lobby"
    time.sleep(0.3)
    assert svc.get_snapshots() == []
    svc.stop()
