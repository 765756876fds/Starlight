"""阶段1：CurrentGameSession 数据链测试（Gameflow → Session → CurrentGamePlayer → history）。"""
import time

import pytest

from app.lol.ongoing.service import OngoingGameService


def _gameflow_session(game_id, me_puuid, me_in_one=True, with_names=True):
    """构造 gameflow /lol-gameflow/v1/session 原始响应。"""
    def cell(i, base, prefix, cid):
        c = {"puuid": f"{prefix}{i}", "summonerId": base + i, "championId": cid + i,
             "cellId": i}
        if with_names:
            c["riotIdGameName"] = prefix.upper() + str(i)
        return c
    t1 = [cell(i, 100, "m", 1) for i in range(5)]
    t2 = [cell(i, 200, "e", 10) for i in range(5)]
    if not me_in_one:
        t1, t2 = t2, t1  # 我方在 teamTwo
    return {"gameId": game_id, "gameData": {"teamOne": t1, "teamTwo": t2}}


class FakeLcu:
    def __init__(self):
        self.phase = "Lobby"
        self.gf_session = None
        self.me_puuid = "m0"
        self.calls = []
        # P0a 断连熔断要求连接状态属性（service._tick 检查 lcu._connected/_session；
        # _session 须非空——空 dict 是 falsy，仍会触发 conn_skip）
        self._connected = True
        self._session = {"connected": True}

    def get(self, endpoint):
        self.calls.append(endpoint)
        if endpoint == "/lol-gameflow/v1/gameflow-phase":
            return self.phase
        if endpoint == "/lol-gameflow/v1/session":
            return self.gf_session
        if endpoint == "/lol-summoner/v1/current-summoner":
            return {"puuid": self.me_puuid}
        if "/lol-summoner/v2/summoners/by-puuid/" in endpoint or "/lol-summoner/v1/summoners/by-puuid/" in endpoint:
            return {"displayName": "P", "summonerLevel": 30, "summonerId": 1}
        if "/lol-champ-select/v1/session" in endpoint:
            return None
        if "/lol-ranked/v1/ranked-stats/" in endpoint:
            return {"queues": [{"queueType": "RANKED_SOLO_5x5",
                                "tier": "GOLD", "division": "II", "leaguePoints": 20}]}
        if "/lol-match-history/v1/" in endpoint:
            return {"games": {"games": []}}
        return None

    def request_raw(self, endpoint):
        return 1, self.get(endpoint)


class FakeAcct:
    def __init__(self):
        self.calls = 0

    def recent(self, puuid, count=None):
        self.calls += 1
        from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE
        return MatchHistoryResult(state=AVAILABLE, records=[
            MatchRecord(game_id=1, game_creation=1, queue_id=420, champion_name="Yasuo",
                        win=True, kills=5, deaths=2, assists=8)
        ])


class FakeLivePlayer:
    def __init__(self, champion_id, kills, deaths, assists, position, level):
        self.champion_id = champion_id
        self.champion_name = ""
        self.raw_champion_name = ""
        self.kills = kills
        self.deaths = deaths
        self.assists = assists
        self.position = position
        self.level = level
        self.team = "ORDER"


class FakeSnap:
    def __init__(self, allies, enemies, game_time=0):
        self.ally_team = allies
        self.enemy_team = enemies
        self.players = allies + enemies
        self.game_time = game_time


def _make_svc(fake, acct=None, snapshot_provider=None):
    return OngoingGameService(
        lcu=fake,
        account_history_service=acct or FakeAcct(),
        snapshot_provider=snapshot_provider,
        poll_interval=0.05, rps=100, max_workers=2,
    )


def _wait_session(svc, game_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = svc.get_session()
        if s is not None and s.game_id == game_id:
            return s
        time.sleep(0.05)
    return svc.get_session()


def test_new_game_id_creates_new_session():
    """gameId A → session A；gameId B → session B；旧 session 立即失效。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(123, "m0")
    svc = _make_svc(fake)
    svc.start()
    try:
        s1 = _wait_session(svc, 123)
        assert s1 is not None
        assert s1.game_id == 123
        assert len(s1.players) == 10
        assert len(s1.my_team) == 5 and len(s1.their_team) == 5

        fake.gf_session = _gameflow_session(456, "m0")  # 新对局
        s2 = _wait_session(svc, 456)
        assert s2 is not None
        assert s2.game_id == 456
        assert s2 is not s1  # 旧 session 对象立即失效，不进入新 session
        assert svc.get_session() is s2
    finally:
        svc.stop()


def test_new_game_force_reload_no_cross_game_reuse():
    """同 puuid 跨局：新 gameId 必须重新请求历史（TTL 不承担 session 隔离）。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(111, "m0")
    acct = FakeAcct()
    svc = _make_svc(fake, acct=acct)
    svc.start()
    try:
        s1 = _wait_session(svc, 111)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if s1 and all(p.history is not None for p in s1.players):
                break
            s1 = svc.get_session()
            time.sleep(0.05)
        n1 = acct.calls
        assert n1 >= 10  # 局1 已为 10 人请求

        fake.gf_session = _gameflow_session(222, "m0")  # 新对局，同 puuid
        s2 = _wait_session(svc, 222)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            s2 = svc.get_session()
            if s2 and all(p.history is not None for p in s2.players):
                break
            time.sleep(0.05)
        assert s2 is not None
        assert s2.game_id == 222
        assert acct.calls > n1  # 新局不跨局复用旧 AVAILABLE 快照，重新请求 SGP
        assert all(p.history is not None for p in s2.players)
        # 新局玩家 history 是新加载对象，不是上一局对象
        old_hist = {p.puuid: p.history for p in s1.players}
        for p in s2.players:
            assert old_hist.get(p.puuid) is not p.history
    finally:
        svc.stop()


def test_team_assignment_me_in_one_vs_two():
    """me ∈ teamOne / me ∈ teamTwo 都必须得到 my_team=5 their_team=5。"""
    fake1 = FakeLcu()
    fake1.phase = "InProgress"
    fake1.me_puuid = "m0"
    fake1.gf_session = _gameflow_session(1, "m0", me_in_one=True)
    svc1 = _make_svc(fake1)
    svc1.start()
    try:
        s = _wait_session(svc1, 1)
        assert len(s.my_team) == 5 and len(s.their_team) == 5
        assert s.my_team[0].puuid == "m0" and s.my_team[0].is_self
        assert s.their_team[0].puuid == "e0"
    finally:
        svc1.stop()

    fake2 = FakeLcu()
    fake2.phase = "InProgress"
    fake2.me_puuid = "e0"
    fake2.gf_session = _gameflow_session(1, "e0", me_in_one=False)  # me 在 teamTwo
    svc2 = _make_svc(fake2)
    svc2.start()
    try:
        s = _wait_session(svc2, 1)
        assert len(s.my_team) == 5 and len(s.their_team) == 5
        assert s.my_team[0].puuid == "e0" and s.my_team[0].is_self
        assert s.their_team[0].puuid == "m0"
    finally:
        svc2.stop()


def test_live_missing_players_still_10():
    """2999 返回 None：Gameflow 10 人仍在（players=10 而不是 0），realtime 标记不可用。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(7, "m0")
    svc = _make_svc(fake, snapshot_provider=lambda: None)
    svc.start()
    try:
        s = _wait_session(svc, 7)
        assert s is not None
        assert len(s.players) == 10
        assert all(not p.realtime_available for p in s.players)
    finally:
        svc.stop()


def test_realtime_merge_identity_unchanged():
    """2999 只更新 KDA/position/level；identity（puuid/team/champion_id）保持不变。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(9, "m0")
    snap = FakeSnap(
        allies=[FakeLivePlayer(1, 3, 1, 5, "TOP", 8)],    # championId=1 → m0
        enemies=[FakeLivePlayer(10, 2, 3, 4, "BOT", 6)],  # championId=10 → e0
    )
    svc = _make_svc(fake, snapshot_provider=lambda: snap)
    svc.start()
    try:
        s = _wait_session(svc, 9)
        assert s is not None
        m0 = next(p for p in s.players if p.puuid == "m0")
        e0 = next(p for p in s.players if p.puuid == "e0")
        # identity 不变（gameflow 权威）
        assert m0.team == "myTeam" and m0.champion_id == 1
        assert e0.team == "theirTeam" and e0.champion_id == 10
        # realtime 更新
        assert m0.live_kills == 3 and m0.live_deaths == 1 and m0.live_assists == 5
        assert m0.live_position == "TOP" and m0.live_level == 8
        assert m0.realtime_available is True
        assert e0.live_kills == 2 and e0.live_level == 6
        assert e0.realtime_available is True
    finally:
        svc.stop()


def test_history_chain_into_player_history():
    """player.puuid → loader → history → CurrentGamePlayer.history（单链）。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(5, "m0")
    acct = FakeAcct()
    svc = _make_svc(fake, acct=acct)
    svc.start()
    try:
        s = _wait_session(svc, 5)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            s = svc.get_session()
            if s and all(p.history is not None for p in s.players):
                break
            time.sleep(0.05)
        assert s is not None
        assert len(s.players) == 10
        assert all(p.history is not None for p in s.players)
        assert all(p.history.data_state == "AVAILABLE" for p in s.players)
        # history 与 identity 的 puuid 一致（同一链）
        for p in s.players:
            assert p.history.puuid == p.puuid
    finally:
        svc.stop()


def test_display_name_backfill_from_history():
    """腾讯服 gameflow cell 缺 riotIdGameName 时：history 加载后回填 snapshot.display_name。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(21, "m0", with_names=False)  # 模拟腾讯服：cell 无名字字段
    svc = _make_svc(fake)
    svc.start()
    try:
        s = _wait_session(svc, 21)
        # gameflow cell 无 riotIdGameName（FakeLcu 返回的 gf cell 不含名字字段）
        deadline = time.time() + 5.0
        while time.time() < deadline:
            s = svc.get_session()
            if s and all(p.display_name for p in s.players):
                break
            time.sleep(0.05)
        # loader→summoner 查询返回 displayName="P"，回填到 identity
        assert all(p.display_name == "P" for p in s.players)
        assert all(p.display_name == p.history.display_name for p in s.players)
    finally:
        svc.stop()


def test_same_game_id_cross_game_still_reloads():
    """腾讯服 gameId 复用：局2 gameId==局1 也必须强制重载（不跨局复用 cache）。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gameflow_session(999, "m0")
    acct = FakeAcct()
    svc = _make_svc(fake, acct=acct)
    svc.start()
    try:
        s1 = _wait_session(svc, 999)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if s1 and all(p.history is not None for p in s1.players):
                break
            s1 = svc.get_session()
            time.sleep(0.05)
        n1 = acct.calls
        assert n1 >= 10

        # 对局结束 → Lobby：清 session（含 last_loaded_key）
        fake.phase = "Lobby"
        time.sleep(0.3)
        assert svc.get_session() is None

        # 新对局，gameId 复用同一个值（腾讯服实测现象）
        fake.phase = "InProgress"
        fake.gf_session = _gameflow_session(999, "m0")
        s2 = _wait_session(svc, 999)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            s2 = svc.get_session()
            if s2 and all(p.history is not None for p in s2.players):
                break
            time.sleep(0.05)
        assert s2 is not None
        assert s2.game_id == 999
        # 必须重新请求 SGP，禁止 history.cache_hit 跨局复用
        assert acct.calls > n1
        assert all(p.history is not None for p in s2.players)
    finally:
        svc.stop()
