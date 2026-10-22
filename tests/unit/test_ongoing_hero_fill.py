"""同局英雄 ID 局部更新 定向测试。

实机根因还原：选人阶段 cs 反解 session（championId=0、summonerId 非 0）发布后，
进游戏 gameflow 提供非 0 championId，但同局分支重建条件（puuid 升级 / 身份升级 /
game_id 填充）全部不满足（我方 5==5、summonerId 5>=5、game_id 两边都有）→
self._session 永不替换 → UI 永远读 champion_id=0 → 英雄显示 ?。

本修：同局分支内把 gameflow 最新有效 championId 局部 0→非0 同步到当前 session
对应玩家（按 puuid 匹配、锁内写入、不触发整 session 重建、不覆盖已有非零值）。

断言基于 session 对象 / 事件计数 / UI 读取入口 get_session()，不依赖 DIAG 文本。
"""
import time

import pytest

pytestmark = pytest.mark.unit

from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE


def _cs_real(my_puuids, game_id=2000, cid=0):
    """选人 cs：明文 puuid、summonerId 非 0（腾讯服行为，仅身份名混淆）、championId=0（未锁定）。"""
    def cell(p, i):
        return {"puuid": p, "summonerId": 100 + i, "championId": cid,
                "cellId": i, "nameVisibilityType": "HIDDEN"}
    return {"gameId": game_id,
            "myTeam": [cell(p, i) for i, p in enumerate(my_puuids)],
            "theirTeam": []}


def _gf_my_only(my_puuids, game_id=2000, base_cid=11):
    """gameflow 只含己方 5 人（敌方 teamTwo 缺失，还原实机）；cid/summonerId 非 0。"""
    def cell(p, i):
        return {"puuid": p, "summonerId": 100 + i, "championId": base_cid + i,
                "cellId": i, "riotIdGameName": f"N{i}"}
    t1 = [cell(p, i) for i, p in enumerate(my_puuids)]
    return {"gameId": game_id, "gameData": {"teamOne": t1, "teamTwo": []}}


class FakeLcu:
    def __init__(self):
        self.phase = "Lobby"
        self.cs_session = None
        self.gf_session = None
        self.me_puuid = "m0"
        self._connected = True
        self._session = {"connected": True}

    def get(self, endpoint):
        if endpoint == "/lol-gameflow/v1/gameflow-phase":
            return self.phase
        if endpoint == "/lol-gameflow/v1/session":
            return self.gf_session
        if endpoint == "/lol-summoner/v1/current-summoner":
            return {"puuid": self.me_puuid}
        if "/lol-champ-select/v1/session" in endpoint:
            return self.cs_session
        if "/lol-summoner/v2/summoners/by-puuid/" in endpoint or "/lol-summoner/v1/summoners/by-puuid/" in endpoint:
            return {"displayName": "P", "summonerLevel": 30, "summonerId": 1}
        if "/lol-ranked/v1/ranked-stats/" in endpoint:
            return {"queues": [{"queueType": "RANKED_SOLO_5x5",
                                "tier": "GOLD", "division": "II", "leaguePoints": 20}]}
        if "/lol-match-history/v1/" in endpoint:
            return {"games": {"games": []}}
        return None

    def request_raw(self, endpoint):
        return 1, self.get(endpoint)


class FakeAcct:
    def recent(self, puuid, count=None):
        return MatchHistoryResult(state=AVAILABLE, records=[
            MatchRecord(game_id=1, game_creation=1, queue_id=420, champion_name="Yasuo",
                        win=True, kills=5, deaths=2, assists=8)
        ])


def _make_svc(fake, events=None):
    return OngoingGameService(
        lcu=fake,
        on_event=(lambda c, d: events.append((c, d))) if events is not None else None,
        account_history_service=FakeAcct(),
        snapshot_provider=None,
        gsm_provider=None,
        deobfuscate_enabled=True,
        poll_interval=0.05, rps=100, max_workers=2,
    )


def _wait(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.03)
    return False


MY = ["m0", "m1", "m2", "m3", "m4"]


def _enter_ingame(fake, svc, gf=None):
    """选人发布后切进游戏（还原实机：cs summonerId 非 0 → 身份升级不触发）。"""
    fake.phase = "InProgress"
    fake.gf_session = gf if gf is not None else _gf_my_only(MY)
    assert _wait(lambda: svc.get_session() is not None
                 and all(p.champion_id for p in svc.get_session().my_team)), \
        "进游戏后己方英雄 ID 未被局部更新"


def test_hero_fill_zero_to_nonzero_ui_reads_value():
    """核心：cs 版本 championId=0 → 进游戏 gameflow 非 0 → 同局不重建 → 局部填充。

    UI 读取入口 = get_session()（UI refresh 的同一入口），断言其返回 player.champion_id
    为 gameflow 提供的正确值；同时断言 session 未重建（无新发布事件、key 不变、
    人数/名字/召唤师ID等身份字段未被触碰）。
    """
    fake = FakeLcu()
    events = []
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake, events=events)
    svc.start()
    try:
        # 选人阶段：session 发布，championId=0
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        s0 = svc.get_session()
        assert all(p.champion_id == 0 for p in s0.my_team), "选人阶段应无英雄 ID"
        key_pre = svc._current_session_key
        created_pre = sum(1 for c, _ in events if c == "ongoing.session_created")

        # 进游戏（同局号）：gameflow 只有己方 5 人、championId 11..15
        _enter_ingame(fake, svc)

        s = svc.get_session()
        assert s.game_id == 2000
        assert svc._current_session_key == key_pre == "gid:2000", "同局 key 不应变化"
        # 每人拿到自己的正确 cid（11+i），不串位
        for i, p in enumerate(s.my_team):
            assert p.puuid == MY[i]
            assert p.champion_id == 11 + i, f"{p.puuid} 英雄 ID 错误: {p.champion_id}"
        # 身份字段未被触碰
        for i, p in enumerate(s.my_team):
            assert p.summoner_id == 100 + i, "summonerId 不应变化"
        assert sum(1 for c, _ in events if c == "ongoing.session_created") == created_pre, \
            "不应创建新 session"
        assert all(p.display_name for p in s.my_team), "名字回填应继续正常"
    finally:
        svc.stop()


def test_hero_fill_idempotent_no_churn():
    """重复 tick：英雄 ID 保持、无新 session、无重建事件。"""
    fake = FakeLcu()
    events = []
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake, events=events)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        key_pre = svc._current_session_key
        created_pre = sum(1 for c, _ in events if c == "ongoing.session_created")
        rebuilt_pre = sum(1 for c, _ in events if c == "ongoing.session_rebuilt")
        _enter_ingame(fake, svc)
        cids = [p.champion_id for p in svc.get_session().my_team]
        # 继续多跑若干 tick（幂等）
        time.sleep(0.6)
        s = svc.get_session()
        assert [p.champion_id for p in s.my_team] == cids, "重复 tick 英雄 ID 不应变化"
        assert svc._current_session_key == key_pre
        assert sum(1 for c, _ in events if c == "ongoing.session_created") == created_pre
        assert sum(1 for c, _ in events if c == "ongoing.session_rebuilt") == rebuilt_pre
    finally:
        svc.stop()


def test_hero_fill_no_overwrite_when_gf_zero():
    """gameflow 临时返回 0：不覆盖已有非零 champion_id。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        _enter_ingame(fake, svc)
        cids = [p.champion_id for p in svc.get_session().my_team]
        assert all(cids), "应已填充"
        # gameflow 临时 championId 归零
        fake.gf_session = _gf_my_only(MY, base_cid=0)
        time.sleep(0.6)
        s = svc.get_session()
        assert [p.champion_id for p in s.my_team] == cids, "已有非零值被 0 覆盖"
    finally:
        svc.stop()


def test_hero_fill_identity_mismatch_skips():
    """玩家身份不匹配（gameflow 缺少某玩家）→ 跳过该玩家，不猜写；其余正常。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        # gameflow 只含 4 人（m0..m3）：m4 无来源 → 保持 0
        gf = _gf_my_only(MY[:4])
        fake.phase = "InProgress"
        fake.gf_session = gf
        assert _wait(lambda: svc.get_session() is not None
                     and sum(1 for p in svc.get_session().my_team if p.champion_id) >= 4), \
            "可匹配玩家应被更新"
        s = svc.get_session()
        by_puuid = {p.puuid: p for p in s.my_team}
        for i in range(4):
            assert by_puuid[MY[i]].champion_id == 11 + i, "匹配玩家应拿到正确 cid"
        assert by_puuid["m4"].champion_id == 0, "无来源玩家应保持 0，不得猜写"
    finally:
        svc.stop()


def test_hero_fill_no_swap_multiple_players():
    """多玩家不串位：各自 cid 与 puuid 一一对应（含乱序 gameflow）。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        # gameflow 乱序返回（m3 在前）
        gf = _gf_my_only(["m3", "m0", "m1", "m2", "m4"])
        fake.phase = "InProgress"
        fake.gf_session = gf
        assert _wait(lambda: svc.get_session() is not None
                     and all(p.champion_id for p in svc.get_session().my_team)), \
            "进游戏后己方英雄 ID 未被局部更新"
        s = svc.get_session()
        by_puuid = {p.puuid: p for p in s.my_team}
        # cid 由 gameflow 下标决定：m3=11, m0=12, m1=13, m2=14, m4=15
        expect = {"m3": 11, "m0": 12, "m1": 13, "m2": 14, "m4": 15}
        for pu, cid in expect.items():
            assert by_puuid[pu].champion_id == cid, f"{pu} 英雄 ID 串位"
    finally:
        svc.stop()


def test_hero_fill_names_stable_after_fill():
    """英雄更新不破坏名字：填充后 display_name 保持非空且与更新前一致。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        assert _wait(lambda: all(p.display_name for p in svc.get_session().my_team)), \
            "选人阶段名字未回填"
        names_pre = {p.puuid: p.display_name for p in svc.get_session().my_team}
        _enter_ingame(fake, svc)
        s = svc.get_session()
        for p in s.my_team:
            assert p.display_name == names_pre[p.puuid], "名字不应被英雄更新改变"
    finally:
        svc.stop()


def test_hero_fill_gf_unavailable_keeps_session():
    """gameflow 不可用（进游戏但 gf 仍 None）：不崩溃，session 保持原状。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        fake.phase = "InProgress"
        fake.gf_session = None
        time.sleep(0.6)
        s = svc.get_session()
        assert s is not None and s.game_id == 2000, "gameflow 不可用不应清空 session"
        assert all(p.champion_id == 0 for p in s.my_team), "无数据源时保持 0（不猜写）"
    finally:
        svc.stop()


def test_hero_fill_enemy_missing_ally_independent():
    """敌方 teamTwo 缺失：己方英雄更新独立工作，敌方不更新、不串入己方。"""
    fake = FakeLcu()
    fake.phase = "ChampSelect"
    fake.cs_session = _cs_real(MY)
    svc = _make_svc(fake)
    svc.start()
    try:
        assert _wait(lambda: svc.get_session() is not None
                     and svc.get_session().game_id == 2000), "选人 session 未建立"
        _enter_ingame(fake, svc)
        s = svc.get_session()
        assert all(p.champion_id for p in s.my_team), "己方应全部填充"
        assert s.their_team == [] or all(p.champion_id == 0 for p in s.their_team), \
            "敌方缺失数据不得伪造"
    finally:
        svc.stop()
