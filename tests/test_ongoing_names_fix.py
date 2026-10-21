"""修 2（P1）：队友名字"?"——by-summonerId 主通道 + retry 补名。

覆盖：
- by-summonerId 200 → display_name = gameName#tagLine 拼接
- summoner 全失败 → 不中断（继续走 history，data_state=AVAILABLE + load_error 标记）
- missing_names_count：统计名字空的 AVAILABLE 快照
- retry 条件扩写：missing_history + missing_names 任一 > 0 即触发
- GSM 补已有玩家名字（尝试性兜底）
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app.lol.ongoing.loader import OngoingGameLoader
from app.lol.ongoing.match_model import AVAILABLE, MatchHistoryResult
from app.lol.ongoing.models import PlayerSnapshot
from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.session import CurrentGameSession
from app.lol.ongoing.gsm_provider import merge_gsm_players


import pytest

pytestmark = pytest.mark.unit


class _FakeAcct:
    """SGP AccountMatchHistoryService 替身：SUMMARY 直接返回 AVAILABLE 空记录。"""

    def recent(self, puuid: str, count: int = 20, summary: bool = False) -> MatchHistoryResult:
        return MatchHistoryResult(state=AVAILABLE, records=[])


class _FakeLCU:
    """按 endpoint 分流返回的 LCU 替身（service._tick 断连熔断需要 _connected/_session）。"""

    _connected = True
    _session = object()

    def __init__(self, resp: dict):
        self.resp = resp
        self.base_url = ""
        self._auth_token = ""

    def get(self, ep: str):
        return self.resp.get(ep)

    def request_raw(self, method, endpoint, data=None):
        return 200, self.resp.get(endpoint)


def _mk_loader(resp: dict) -> OngoingGameLoader:
    return OngoingGameLoader(
        lcu_get=_FakeLCU(resp).get,
        account_history_service=_FakeAcct(),
        recent_matches_count=10,
        on_event=lambda c, d: None,
    )


# ---------- by-summonerId 主通道 ----------

def test_load_one_by_summoner_id():
    """InProgress 阶段 summonerId 真实 → by-id 200 → gameName#tagLine 拼接。"""
    resp = {
        "/lol-summoner/v2/summoners/by-puuid/p1": None,
        "/lol-summoner/v1/summoners/by-puuid/p1": None,
        "/lol-summoner/v1/summoners/4106986647833440": {
            "summonerId": 4106986647833440,
            "gameName": "澹然独与神明居",
            "tagLine": "17972",
            "displayName": "",
            "summonerLevel": 42,
        },
        "/lol-ranked/v1/ranked-stats/p1": {"queues": []},
    }
    loader = _mk_loader(resp)
    snap = loader._load_one({
        "puuid": "p1",
        "summoner_id": 4106986647833440,
        "cell_id": 1,
        "team": "myTeam",
        "assigned_position": "MID",
        "display_name": "",
        "champion_id": 67,
    })
    assert snap is not None
    assert snap.display_name == "澹然独与神明居#17972"
    assert snap.data_state == AVAILABLE
    assert snap.load_error is None


# ---------- summoner 全失败 → 不中断 ----------

def test_load_one_summoner_failed_continues():
    """by-puuid + by-id 全 404 → summoner_failed 标记 + 继续走 history（不 return None）。"""
    resp = {
        "/lol-summoner/v2/summoners/by-puuid/p1": None,
        "/lol-summoner/v1/summoners/by-puuid/p1": None,
        "/lol-summoner/v1/summoners/1": None,
        "/lol-ranked/v1/ranked-stats/p1": {"queues": []},
    }
    loader = _mk_loader(resp)
    snap = loader._load_one({
        "puuid": "p1",
        "summoner_id": 1,
        "cell_id": 2,
        "team": "myTeam",
        "assigned_position": "TOP",
        "display_name": "",
        "champion_id": 86,
    })
    assert snap is not None                  # 不再 return None
    assert snap.display_name == ""           # 名字缺失（等 retry 补）
    assert snap.data_state == AVAILABLE      # history 成功
    assert snap.load_error == "summoner_not_found"


# ---------- missing_names_count ----------

def test_missing_names_count():
    """只统计 AVAILABLE 且名字空的快照；有名字的不计。"""
    loader = _mk_loader({})
    now = time.monotonic()
    loader._cache.put(PlayerSnapshot(puuid="p1", display_name="", data_state="AVAILABLE",
                                     loaded_at=now))
    loader._cache.put(PlayerSnapshot(puuid="p2", display_name="有名字#1234", data_state="AVAILABLE",
                                     loaded_at=now))
    loader._cache.put(PlayerSnapshot(puuid="p3", display_name="", data_state="ERROR",
                                     loaded_at=now))
    assert loader.missing_names_count() == 1   # 只 p1（AVAILABLE + 空名）


# ---------- retry 条件（missing 任一 > 0 即触发） ----------

def test_retry_triggered_on_missing_names(monkeypatch):
    """history 不缺但名字缺 → missing=2 → retry 触发补名重载。"""
    fake_lcu = _FakeLCU({
        "/lol-gameflow/v1/gameflow-phase": "InProgress",
        "/lol-gameflow/v1/session": {
            "gameId": 123,
            "phase": "InProgress",
            "gameData": {
                "teamOne": [{"puuid": "me", "summonerId": 1, "championId": 67}],
                "teamTwo": [],
            },
        },
        "/lol-summoner/v1/current-summoner": {"puuid": "me"},
    })
    svc = OngoingGameService(lcu=fake_lcu, on_event=lambda c, d: None,
                             deobfuscate_enabled=False)
    svc._session = CurrentGameSession(game_id=123, phase="InProgress")
    svc._current_session_key = "gid:123"
    svc._loading_in_progress = False
    svc._retry_state = {}
    svc._loader = SimpleNamespace(
        missing_history_count=lambda: 0,
        missing_names_count=lambda: 2,
    )
    spawned = []
    monkeypatch.setattr(svc, "_load_in_thread", lambda snapshot: spawned.append(snapshot))
    svc._tick()
    assert len(spawned) == 1                 # 名字缺失 → retry 触发


# ---------- GSM 补已有玩家名字 ----------

def test_gsm_supplement_existing_player_name():
    """已有玩家名字字段全空时，用 GSM summonerName 补名；有名字/无 puuid 不动；GSM 独有玩家正常追加。"""
    session = {
        "gameId": 9,
        "myTeam": [
            {"puuid": "me", "summonerId": 0, "name": "", "championId": 67},
            {"puuid": "p1", "summonerId": 1, "name": "", "championId": 67},
            {"puuid": "p2", "summonerId": 2, "name": "已有名字", "championId": 86},
            {"summonerId": 3, "name": "", "championId": 0},   # 无 puuid，不动
        ],
        "theirTeam": [],
    }
    gsm_game = {
        "game": {
            "id": 9,   # _gsm_game_id 优先 playerCredentials.gameId，其次 game.id
            "teamOne": [
                {"puuid": "me", "summonerName": "我"},
                {"puuid": "p1", "summonerName": "GSM补的名字"},
                {"puuid": "p2", "summonerName": "不该覆盖的名字"},
            ],
            "teamTwo": [
                {"puuid": "p9", "summonerName": "新玩家"},
            ],
        },
    }
    merged, added, ok = merge_gsm_players(session, gsm_game, me_puuid="me")
    assert ok
    assert added == 1                                    # 只追加 GSM 独有玩家 p9
    cells = merged["myTeam"]
    assert cells[0]["riotIdGameName"] == "我"            # me 空名 → GSM 也补
    assert cells[1]["riotIdGameName"] == "GSM补的名字"   # p1 空名 → 补
    assert cells[2]["name"] == "已有名字"               # p2 有名字 → 不动
    assert cells[3]["summonerId"] == 3                  # 无 puuid → 不动
    assert len(merged["theirTeam"]) == 1                # p9 追加
