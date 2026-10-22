"""Phase 1A: 统一历史战绩数据链 + 10 人房间分析。"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.lol.ongoing.loader import OngoingGameLoader
from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE, NO_DATA, ERROR
from app.lol.ongoing.account_history import AccountMatchHistoryService


def mk_rec(win, champ="A", k=3, d=2, a=5, pos="MID", creation=None):
    # queue_id=420（单双排）：与 loader 白名单口径对齐（_VALID_QUEUE_IDS 含 400/420/430/440/460/490），
    # 默认 0 会被过滤导致 data_state=NO_DATA（测试曾因夹具未设 queue_id 而误判 NO_DATA）。
    return MatchRecord(game_id=creation or int(time.time()*1000), champion_name=champ,
                       win=win, kills=k, deaths=d, assists=a, team_position=pos,
                       game_creation=creation or int(time.time()*1000), queue_id=420)


class FakeAcct:
    def __init__(self, data=None, fail=None):
        self.data = data or {}
        self.fail = fail or set()
    def recent(self, puuid, count=None):
        if puuid in self.fail:
            return MatchHistoryResult(state=ERROR, message="boom")
        recs = self.data.get(puuid)
        if recs is None:
            return MatchHistoryResult(state=NO_DATA)
        return MatchHistoryResult(state=AVAILABLE, records=list(recs), total=len(recs))


def fake_lcu(session=None):
    def get(ep):
        if ep == "/lol-champ-select/v1/session":
            return session
        if "summoners/by-puuid/" in ep:
            p = ep.rsplit("/", 1)[-1]
            return {"puuid": p, "displayName": "P_" + p[-4:], "summonerLevel": 30}
        if "ranked-stats" in ep:
            return {"queues": [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD",
                               "division": "I", "leaguePoints": 20}]}
        return None
    return get


SESSION = {
    "myTeam": [
        {"puuid": "puuid_aaaa1", "cellId": 0, "name": "A1", "assignedPosition": "mid"},
        {"puuid": "puuid_aaaa2", "cellId": 1, "name": "A2"},
    ],
    "theirTeam": [
        {"puuid": "puuid_bbbb1", "cellId": 0, "name": "B1"},
        {"puuid": "puuid_bbbb2", "cellId": 1, "name": "B2"},
    ],
}


def test_loader_uses_account_service():
    recs = [mk_rec(True, "Ahri", creation=3), mk_rec(True, "Ahri", creation=2),
            mk_rec(False, "Yasuo", creation=1)]
    acct = FakeAcct(data={"puuid_aaaa1": recs})
    ld = OngoingGameLoader(fake_lcu(SESSION), account_history_service=acct)
    snaps = ld.load_for_champ_select(SESSION)
    by = {s.puuid: s for s in snaps}
    a1 = by["puuid_aaaa1"]
    assert a1.data_state == AVAILABLE
    assert abs(a1.win_rate - 2/3) < 0.01
    assert a1.win_streak == 2  # latest two wins
    assert a1.top_champions == ["Ahri", "Yasuo"]
    assert a1.main_position == "MID"
    print("PASS loader_uses_account_service")


def test_failure_isolation_one_player():
    acct = FakeAcct(data={"puuid_aaaa1": [mk_rec(True)]}, fail={"puuid_aaaa2"})
    ld = OngoingGameLoader(fake_lcu(SESSION), account_history_service=acct)
    snaps = ld.load_for_champ_select(SESSION)
    assert len(snaps) == 4
    states = {s.puuid: s.data_state for s in snaps}
    assert states["puuid_aaaa1"] == AVAILABLE
    assert states["puuid_aaaa2"] == ERROR
    assert states["puuid_bbbb1"] == NO_DATA
    print("PASS failure_isolation")


def test_duplicate_puuid_dedup():
    sess = {"myTeam": [{"puuid": "dup", "cellId": 0}, {"puuid": "dup", "cellId": 1}],
            "theirTeam": []}
    acct = FakeAcct(data={"dup": [mk_rec(True)]})
    ld = OngoingGameLoader(fake_lcu(sess), account_history_service=acct)
    snaps = ld.load_for_champ_select(sess)
    assert len(snaps) == 1
    print("PASS dedup")


def test_bot_no_data_legal():
    acct = FakeAcct()  # nothing
    ld = OngoingGameLoader(fake_lcu(SESSION), account_history_service=acct)
    snaps = ld.load_for_champ_select(SESSION)
    assert all(s.data_state == NO_DATA for s in snaps)
    print("PASS bot_no_data")


def test_streak_loss_and_empty():
    recs_loss = [mk_rec(False, "X", creation=2), mk_rec(False, "X", creation=1), mk_rec(True, "X", creation=0)]
    assert OngoingGameLoader._streak(recs_loss) == -2
    assert OngoingGameLoader._streak([]) == 0
    print("PASS streak")


def test_kda_zero_deaths():
    recs = [mk_rec(True, "A", k=5, d=0, a=5)]
    top, pos = OngoingGameLoader._summarize(recs)
    assert top == ["A"] and pos == "MID"
    print("PASS summarize")


if __name__ == "__main__":
    test_loader_uses_account_service()
    test_failure_isolation_one_player()
    test_duplicate_puuid_dedup()
    test_bot_no_data_legal()
    test_streak_loss_and_empty()
    test_kda_zero_deaths()
    print("ALL PASS")
