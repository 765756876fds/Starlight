"""对局加载断链修复测试（根因 A）：进游戏人数突变强制重载 + 缺失 puuid 计入重试。

覆盖：
- loader.missing_history_count(session_puuids)：cache 无快照的 session puuid 计入缺失
- service：选人 5 puuid → 进游戏 gameflow 10 puuid（同 game_id）→ 强制重建 + 补拉
- 重建保留已加载 history（old_by_puuid 匹配）；回归新 session 正常创建
"""
import time

import pytest

from app.lol.ongoing.cache import SnapshotCache
from app.lol.ongoing.loader import OngoingGameLoader
from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE, ERROR


def _puuid_session(my_puuids, their_puuids, game_id=601006257268):
    """构造 gameflow 响应（按给定 puuid 集合）。"""
    def cell(p, base, cid):
        return {"puuid": p, "summonerId": base, "championId": cid, "cellId": 0}
    t1 = [cell(p, 100 + i, 1 + i) for i, p in enumerate(my_puuids)]
    t2 = [cell(p, 200 + i, 10 + i) for i, p in enumerate(their_puuids)]
    return {"gameId": game_id, "gameData": {"teamOne": t1, "teamTwo": t2}}


class FakeLcu:
    """模拟 LCU：phase / cs session / gameflow session 可变（先选人后进游戏）。"""
    def __init__(self):
        self.phase = "Lobby"
        self.cs_session = None
        self.gf_session = None
        self.me_puuid = "m0"
        self.calls = []
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
    def __init__(self):
        self.calls = 0

    def recent(self, puuid, count=None):
        self.calls += 1
        return MatchHistoryResult(state=AVAILABLE, records=[
            MatchRecord(game_id=1, game_creation=1, queue_id=420, champion_name="Yasuo",
                        win=True, kills=5, deaths=2, assists=8)
        ])


def _make_svc(fake, acct=None, cache=None):
    acct = acct or FakeAcct()
    return OngoingGameService(
        lcu=fake,
        account_history_service=acct,
        snapshot_provider=None,
        gsm_provider=None,
        deobfuscate_enabled=True,
        poll_interval=0.05, rps=100, max_workers=2,
    )


def _wait(cond, timeout=4.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------- loader.missing_history_count ----------

class TestMissingHistoryCount:
    def test_no_cache_session_missing_counts(self):
        l = OngoingGameLoader(lcu_get=lambda ep: None)
        assert l.missing_history_count({"a", "b", "c"}) == 3

    def test_error_snapshots_plus_missing(self):
        cache = SnapshotCache(ttl_seconds=300)
        l = OngoingGameLoader(lcu_get=lambda ep: None, cache=cache)
        # 2 个 ERROR 快照（已缓存但请求失败）
        s1 = _snap("p1", ERROR)
        s2 = _snap("p2", ERROR)
        cache.put(s1)
        cache.put(s2)
        # session 有 5 个 puuid：p1/p2 已缓存（ERROR 计入），p3-p5 从未缓存（缺失计入）
        assert l.missing_history_count({"p1", "p2", "p3", "p4", "p5"}) == 5

    def test_no_data_not_counted(self):
        l = OngoingGameLoader(lcu_get=lambda ep: None)
        # NO_DATA 是业务事实，不触发重试
        assert l.missing_history_count(None) == 0

    def test_backward_compat_no_session_arg(self):
        cache = SnapshotCache(ttl_seconds=300)
        l = OngoingGameLoader(lcu_get=lambda ep: None, cache=cache)
        cache.put(_snap("p1", ERROR))
        assert l.missing_history_count() == 1  # 旧调用：只算 ERROR


def _snap(puuid, state):
    from app.lol.ongoing.models import PlayerSnapshot
    s = PlayerSnapshot(puuid=puuid)
    s.data_state = state
    s.loaded_at = time.time()  # cache.all() 的 TTL 过滤依赖真实加载时间
    return s


# ---------- service：选人 5 → 进游戏 10 强制重建 ----------

class TestServiceForceRebuild:
    def test_ingame_puuid_surge_triggers_rebuild_and_reload(self):
        """选人阶段只 resolve 己方 5 puuid → 进游戏 gameflow 10 人全 puuid（同 game_id）
        → 强制重建 session（10 puuid）+ 自动补拉缺失玩家。"""
        fake = FakeLcu()
        # 选人阶段：己方 5 人明文 puuid，敌方 5 人 HIDDEN（puuid 空、反解失败）
        fake.phase = "ChampSelect"
        my_p = [f"m{i}" for i in range(5)]
        fake.cs_session = {
            "gameId": 601006257268,
            "myTeam": [{"puuid": p, "summonerId": 100 + i, "championId": 1 + i, "cellId": i}
                       for i, p in enumerate(my_p)],
            "theirTeam": [{"puuid": "", "summonerId": 0, "championId": 10 + i, "cellId": i,
                           "nameVisibilityType": "HIDDEN", "obfuscatedPuuid": ""}
                          for i in range(5)],
        }
        acct = FakeAcct()
        svc = _make_svc(fake, acct=acct)
        svc.start()
        try:
            # 选人阶段：session 建立（只有 5 个 puuid）
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 5), "选人 session 未建立"
            s_pre = svc.get_session()
            assert s_pre.game_id == 601006257268

            # 进游戏：gameflow 10 人全 puuid，同 game_id
            fake.phase = "InProgress"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=601006257268)
            # 等待重建：10 人全 puuid
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 10), \
                "进游戏未触发强制重建（10 puuid）"

            s_post = svc.get_session()
            assert len(s_post.players) == 10
            # 己方 5 人 history 保留（old_by_puuid 复用）
            my_puuids = {p.puuid for p in s_post.my_team}
            my_hist = [p.history for p in s_post.players if p.puuid in my_puuids]
            assert all(h is not None for h in my_hist), "己方 history 未被保留"

            # 补拉完成：loader cache 达到 10 人（5 缓存复用 + 5 新请求）
            assert _wait(lambda: svc._loader._cache.size() == 10), "缺失 5 人未被补拉"
        finally:
            svc.stop()

    def test_same_puuid_no_surge_no_reload(self):
        """同一局 puuid 数不变 → 不触发重建/补拉（回归：不打断已加载会话）。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=601006257268)
        acct = FakeAcct()
        svc = _make_svc(fake, acct=acct)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 10), "初始 10 人未建立"
            n0 = acct.calls
            # 继续 tick（同 session、puuid 数不变）→ 不触发新加载
            time.sleep(0.4)
            assert acct.calls == n0, "puuid 数未变却触发了重复加载"
        finally:
            svc.stop()

    def test_new_game_id_creates_new_session(self):
        """不同 game_id → 新 session（回归：跨局隔离不受影响）。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=123)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None and svc.get_session().game_id == 123)
            assert len(svc.get_session().players) == 10
        finally:
            svc.stop()


# ---------- 2026-10-18：选人阶段残留上一把队友清理 ----------

def _cs_session(my_puuids, their_puuids, game_id=999):
    """构造 champ-select session（明文 puuid，deobfuscate 直接通过）。"""
    def cell(p, cid):
        return {"puuid": p, "summonerId": 0, "championId": cid,
                "cellId": 0, "nameVisibilityType": "HIDDEN"}
    my = [cell(p, 1 + i) for i, p in enumerate(my_puuids)]
    their = [cell(p, 10 + i) for i, p in enumerate(their_puuids)]
    return {"gameId": game_id, "myTeam": my, "theirTeam": their}


class TestChampSelectCacheClear:
    """新选人阶段严禁残留上一把队友（UI 显示上一把 = 体验 bug）。

    场景：上一把对局中（InProgress）→ 直接切到新选人（ChampSelect）而未经
    清理分支（训练营重开 / phase 跳变 / 新选人 session 构建失败）→
    _session 必须立即清空，等新 session 构建成功再填充。
    """

    def test_prev_game_session_cleared_when_new_champ_select_build_fails(self):
        """上一把 InProgress → 新选人构建失败（gameflow+cs 均未就绪）→ 清空。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=601006257268)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None), "上一把 session 未建立"
            assert len(svc.get_session().players) == 10
            # 新选人：gameflow 未就绪 + cs session 未就绪 → 清空（严禁残留）
            fake.phase = "ChampSelect"
            fake.gf_session = None
            fake.cs_session = None
            assert _wait(lambda: svc.get_session() is None), "上一把 session 未清空（残留 bug）"
            assert svc._current_session_key is None
        finally:
            svc.stop()

    def test_new_champ_select_rebuilds_session_after_clear(self):
        """清理后 cs session 反解成功 → 新 session 重新构建（不误杀新局）。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=601006257268)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None), "上一把 session 未建立"
            # 新选人：cs session 就绪（新 gameId）→ 清空后重新构建新 session
            fake.phase = "ChampSelect"
            fake.gf_session = None
            fake.cs_session = _cs_session(["m0", "q1", "q2", "q3", "q4"],
                                          ["r1", "r2", "r3", "r4", "r5"],
                                          game_id=888)
            assert _wait(lambda: (svc.get_session() is not None
                                  and svc.get_session().game_id == 888)), "新选人 session 未构建"
            assert len(svc.get_session().players) == 10
        finally:
            svc.stop()

    def test_game_start_no_false_clear(self):
        """GameStart 阶段 session 非空不清空（回归：同局 phase 演进不受影响）。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=601006257268)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None), "session 未建立"
            fake.phase = "GameStart"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=601006257268)
            time.sleep(0.4)
            assert svc.get_session() is not None, "GameStart 被误清空"
        finally:
            svc.stop()
