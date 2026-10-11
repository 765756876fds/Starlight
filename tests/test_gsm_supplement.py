"""阶段2：GSM 补充身份源测试（Gameflow 主源 → GSM supplement → puuid merge → CurrentGameSession）。"""
import time

import pytest

from app.lol.ongoing.service import OngoingGameService


def _gf(me_puuid, t1_count=5, t2_count=5, no_puuid_idx=None, game_id=123):
    """构造 gameflow /lol-gameflow/v1/session 原始响应（me 在 teamOne）。"""
    t1 = [{"puuid": f"m{i}", "summonerId": 100 + i, "championId": 1 + i, "cellId": i}
          for i in range(t1_count)]
    t2 = []
    for i in range(t2_count):
        c = {"puuid": f"e{i}", "summonerId": 200 + i, "championId": 10 + i, "cellId": i}
        if no_puuid_idx is not None and i == no_puuid_idx:
            c["puuid"] = ""
        t2.append(c)
    return {"gameId": game_id, "gameData": {"teamOne": t1, "teamTwo": t2}}


def _gsm_member(puuid, sid, cid):
    return {"puuid": puuid, "summonerId": sid, "championId": cid, "selectedPosition": "BOT"}


def _gsm_game(game_id, t1=None, t2=None):
    return {"game": {"teamOne": t1 or [], "teamTwo": t2 or []},
            "playerCredentials": {"gameId": game_id}}


def _gsm_t1():
    return [_gsm_member(f"m{i}", 1000 + i, 101 + i) for i in range(5)]


class FakeLcu:
    def __init__(self):
        self.phase = "InProgress"
        self.gf_session = None
        self.me_puuid = "m0"

    def get(self, endpoint):
        if endpoint == "/lol-gameflow/v1/gameflow-phase":
            return self.phase
        if endpoint == "/lol-gameflow/v1/session":
            return self.gf_session
        if endpoint == "/lol-summoner/v1/current-summoner":
            return {"puuid": self.me_puuid}
        return None

    def request_raw(self, endpoint):
        return 1, self.get(endpoint)


class FakeGsm:
    def __init__(self):
        self.status = "ok"
        self.game = None
        self.calls = []
        self.raise_error = False

    def get_current_game(self, self_puuid):
        self.calls.append(self_puuid)
        if self.raise_error:
            raise RuntimeError("gsm boom")
        return self.status, self.game


def _make_svc(fake, gsm=None, poll=0.05):
    return OngoingGameService(
        lcu=fake,
        account_history_service=None,
        snapshot_provider=None,
        gsm_provider=gsm,
        poll_interval=poll,
    )


def _wait_session(svc, game_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = svc.get_session()
        if s is not None and s.game_id == game_id:
            return s
        time.sleep(0.05)
    return svc.get_session()


def test_gsm_supplements_missing_enemy():
    """GF 5+4（敌方缺1）+ GSM 5+5 → 最终 10 人，第 5 敌来自 GSM。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=4)  # GF 只有 4 个敌方
    gsm = FakeGsm()
    gsm.game = _gsm_game(123, t1=_gsm_t1(),
                         t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(4)]
                         + [_gsm_member("E4", 9999, 777)])  # GSM 第 5 敌 E4
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert s is not None
        assert len(s.players) == 10          # 5+4 GF + 1 GSM 补
        assert len(s.my_team) == 5 and len(s.their_team) == 5
        e4 = next((p for p in s.their_team if p.puuid == "E4"), None)
        assert e4 is not None                # GSM 独有敌人已追加
        assert e4.champion_id == 777
    finally:
        svc.stop()


def test_gsm_does_not_override_existing():
    """GSM 与 GF 同 puuid 但字段不同 → 保留 GF 原值，不覆盖。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=5)
    gsm = FakeGsm()
    # GSM 敌方同 puuid 但 championId 完全不同（999），summonerId 也不同
    gsm.game = _gsm_game(123, t1=_gsm_t1(),
                         t2=[_gsm_member(f"e{i}", 8888 + i, 999 + i) for i in range(5)])
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert len(s.players) == 10          # 不追加（无新 puuid）
        e0 = next(p for p in s.their_team if p.puuid == "e0")
        assert e0.champion_id == 10          # GF 原值 10，未被 GSM 的 999 覆盖
        assert e0.summoner_id == 200         # GF 原值
    finally:
        svc.stop()


def test_gsm_same_puuid_not_duplicated():
    """GSM 6 个敌人（5 重合 + 1 新）→ 只追加 1，无重复 puuid。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=5)
    gsm = FakeGsm()
    gsm.game = _gsm_game(123, t1=_gsm_t1(),
                         t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(5)]
                         + [_gsm_member("E5", 6000, 601)])
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert len(s.players) == 11          # 10 原 + 1 新
        puuids = [p.puuid for p in s.players]
        assert len(set(puuids)) == len(puuids)  # 无重复
        assert next((p for p in s.their_team if p.puuid == "E5"), None) is not None
    finally:
        svc.stop()


def test_gsm_only_appends_gsm_exclusive_puuid():
    """merge key=puuid：GSM 中与 GF 重合的不动，只追加 GSM 独有 puuid。"""
    from app.lol.ongoing.gsm_provider import merge_gsm_players
    gf_sess = {"gameId": 123,
               "myTeam": [{"puuid": f"m{i}", "championId": 1 + i} for i in range(5)],
               "theirTeam": [{"puuid": f"e{i}", "championId": 10 + i} for i in range(4)]}
    gsm_game = _gsm_game(
        123,
        t1=[_gsm_member(f"m{i}", 1000 + i, 101 + i) for i in range(5)],
        t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(4)]
        + [_gsm_member("E9", 7000, 709)],
    )
    merged, added, matched = merge_gsm_players(gf_sess, gsm_game, "m0")
    assert matched is True
    assert added == 1
    assert [c["puuid"] for c in merged["theirTeam"]] == ["e0", "e1", "e2", "e3", "E9"]
    assert [c["puuid"] for c in merged["myTeam"]] == ["m0", "m1", "m2", "m3", "m4"]


def test_gsm_failure_degrades_to_gameflow():
    """GSM 404 / no_token / 抛异常 → session 保持纯 Gameflow（9 人），不消失不空。"""
    for status in ("http_404", "no_token", "resolver_failed", "http_error", "bad_json"):
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _gf("m0", t1_count=5, t2_count=4)  # GF=9 人
        gsm = FakeGsm()
        gsm.status = status
        gsm.game = _gsm_game(123, t1=_gsm_t1(),
                             t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(5)])
        svc = _make_svc(fake, gsm=gsm)
        svc.start()
        try:
            s = _wait_session(svc, 123)
            assert s is not None
            assert len(s.players) == 9       # 原 Gameflow 结果，无补人
            assert len(s.my_team) == 5 and len(s.their_team) == 4
        finally:
            svc.stop()

    # 异常路径
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=4)
    gsm = FakeGsm()
    gsm.raise_error = True
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert s is not None and len(s.players) == 9
    finally:
        svc.stop()


def test_gsm_game_id_mismatch_no_merge():
    """GSM gameId ≠ GF gameId / GSM gameId 缺失 → 不 merge（禁止跨局污染）。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=4, game_id=123)
    gsm = FakeGsm()
    gsm.game = _gsm_game(999, t1=_gsm_t1(),
                         t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(5)])
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert len(s.players) == 9          # gameId 不一致 → 原样
    finally:
        svc.stop()

    # GSM gameId 缺失
    fake2 = FakeLcu()
    fake2.phase = "InProgress"
    fake2.gf_session = _gf("m0", t1_count=5, t2_count=4, game_id=123)
    gsm2 = FakeGsm()
    gsm2.game = {"game": {"teamOne": _gsm_t1(),
                          "teamTwo": [_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(5)]},
                 "playerCredentials": {}}
    svc2 = _make_svc(fake2, gsm=gsm2)
    svc2.start()
    try:
        s2 = _wait_session(svc2, 123)
        assert len(s2.players) == 9
    finally:
        svc2.stop()


def test_gsm_phase_gamestart_and_reconnect():
    """GameStart / Reconnect 均启用 GSM（GF 5+4 → 10 人）。"""
    for phase in ("GameStart", "Reconnect"):
        fake = FakeLcu()
        fake.phase = phase
        fake.gf_session = _gf("m0", t1_count=5, t2_count=4)
        gsm = FakeGsm()
        gsm.game = _gsm_game(123, t1=_gsm_t1(),
                             t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(4)]
                             + [_gsm_member("E4", 9999, 777)])
        svc = _make_svc(fake, gsm=gsm)
        svc.start()
        try:
            s = _wait_session(svc, 123)
            assert s is not None
            assert len(s.players) == 10
            assert len(s.their_team) == 5
        finally:
            svc.stop()


def test_gsm_no_puuid_cell_not_converted_by_champion_id():
    """GF 无 puuid cell 不因 GSM 凭 championId 被激活；GSM 以独立 puuid 身份追加。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    # GF teamTwo 第 5 个（e4）无 puuid（cid=14）；gameflow 过滤后 their=4
    fake.gf_session = _gf("m0", t1_count=5, t2_count=5, no_puuid_idx=4)
    gsm = FakeGsm()
    # GSM 第 5 敌 E4 的 championId 与 GF 无 puuid cell 相同（14），但有独立明文 puuid
    gsm.game = _gsm_game(123, t1=_gsm_t1(),
                         t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(4)]
                         + [_gsm_member("e4new", 9999, 14)])
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert s is not None
        assert all(p.puuid for p in s.players)         # 无空 puuid 玩家
        cid14 = [p for p in s.players if p.champion_id == 14]
        assert len(cid14) == 1                          # 只有 1 个 cid=14（GSM 补的身份）
        assert cid14[0].puuid == "e4new"                # 不是空 puuid cell 被"激活"
        assert len(s.their_team) == 5
    finally:
        svc.stop()


def test_gsm_not_requested_in_champ_select():
    """ChampSelect 阶段绝不请求 GSM（即使 gameflow session 可用）。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=5)
    gsm = FakeGsm()
    gsm.game = _gsm_game(123, t1=_gsm_t1(),
                         t2=[_gsm_member(f"e{i}", 2000 + i, 500 + i) for i in range(5)])
    svc = _make_svc(fake, gsm=gsm)
    svc.start()
    try:
        time.sleep(0.4)          # 多个 tick 周期
        assert gsm.calls == []   # GSM 未被请求
        s = svc.get_session()
        assert s is not None and len(s.players) == 10   # 纯 Gameflow 10 人
    finally:
        svc.stop()


def test_gsm_provider_not_configured_keeps_legacy_behavior():
    """未配置 gsm_provider（None）→ 与阶段1完全一致（GF 5+4 → 9 人）。"""
    fake = FakeLcu()
    fake.phase = "InProgress"
    fake.gf_session = _gf("m0", t1_count=5, t2_count=4)
    svc = _make_svc(fake, gsm=None)
    svc.start()
    try:
        s = _wait_session(svc, 123)
        assert s is not None
        assert len(s.players) == 9
        assert len(s.my_team) == 5 and len(s.their_team) == 4
    finally:
        svc.stop()
