"""对局信息拉取链路身份修复测试（根因 B/C）。

覆盖：
- 选人阶段同局连续 tick 不清空 session（消除每秒清空→重建→force 重拉的瀑布式加载）
- 进游戏 gameflow 与选人同 puuid 数、但 summonerId 升级（0 → 真实）→ 强制重建 + 重查名字
  （修复 UI "? Lv.?"：选人 HIDDEN cell summonerId=0 → by-id 无法查名字）
- loader：NO_DATA 空名计入 missing_names_count；空名快照（任意状态）不作为缓存复用
- 选人 deobfuscate 分队诊断事件（ongoing.cs_team_resolve）
"""
import time

from app.lol.ongoing.cache import SnapshotCache
from app.lol.ongoing.loader import OngoingGameLoader
from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE, NO_DATA


# ---------- 桩 ----------

def _cs_session_5(my_puuids, game_id=888):
    """ARAM/人机局风格选人 session：只有己方 5 人，全部 HIDDEN（summonerId=0）。"""
    return {
        "gameId": game_id,
        "myTeam": [
            {"puuid": p, "summonerId": 0, "championId": 1 + i, "cellId": i,
             "nameVisibilityType": "HIDDEN", "obfuscatedPuuid": ""}
            for i, p in enumerate(my_puuids)
        ],
        "theirTeam": [],
    }


def _gf_session_5(my_puuids, game_id=888, sid_base=300):
    """进游戏 gameflow：同 5 人、真实 summonerId（选人时为 0）。"""
    return {
        "gameId": game_id,
        "gameData": {
            "teamOne": [
                {"puuid": p, "summonerId": sid_base + i, "championId": 1 + i, "cellId": i}
                for i, p in enumerate(my_puuids)
            ],
            "teamTwo": [],
        },
    }


class FakeLcu:
    """模拟 LCU：by-puuid 一律 404（腾讯服禁用），by-summonerId 可查（真实名字）。"""

    def __init__(self):
        self.phase = "Lobby"
        self.cs_session = None
        self.gf_session = None
        self.me_puuid = "m0"
        self.by_id_names = {300 + i: f"队友{i}#TAG" for i in range(5)}
        self.by_id_names[300] = "我自己#TAG"
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
            return None  # 腾讯服全局禁用 by-puuid
        if "/lol-ranked/v1/ranked-stats/" in endpoint:
            return {"queues": [{"queueType": "RANKED_SOLO_5x5",
                                "tier": "GOLD", "division": "II", "leaguePoints": 20}]}
        if "/lol-match-history/v1/" in endpoint:
            return {"games": {"games": []}}
        # by-summonerId: /lol-summoner/v2/summoners/<sid> 或 /lol-summoner/v1/summoners/<sid>
        for prefix in ("/lol-summoner/v2/summoners/", "/lol-summoner/v1/summoners/"):
            if endpoint.startswith(prefix):
                sid = int(endpoint[len(prefix):])
                name = self.by_id_names.get(sid)
                if name:
                    return {"displayName": name.split("#")[0], "gameName": name.split("#")[0],
                            "tagLine": name.split("#")[1], "summonerLevel": 33, "summonerId": sid}
                return None
        return None

    def request_raw(self, endpoint):
        return 1, self.get(endpoint)


class FakeAcct:
    def __init__(self, state=AVAILABLE):
        self.calls = 0
        self.state = state

    def recent(self, puuid, count=None):
        self.calls += 1
        return MatchHistoryResult(state=self.state, records=[
            MatchRecord(game_id=1, game_creation=1, queue_id=420, champion_name="Yasuo",
                        win=True, kills=5, deaths=2, assists=8)
        ])


def _make_svc(fake, acct=None):
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


def _codes(events):
    return [c for c, _ in events]


# ---------- 根因 B：选人阶段同局连续 tick 不再清空重建 ----------

class TestChampSelectNoChurn:
    def test_same_game_champ_select_ticks_keep_session_and_no_extra_load(self):
        """同 game_id 的连续 ChampSelect tick：session 稳定不清空、不重复拉取。

        回归：旧实现在每个选人 tick 清空 session → 每秒重建 + force 重拉全部玩家
        （日志中 ongoing.started/history.request 每秒重复、cache_skip 瀑布）。
        """
        fake = FakeLcu()
        fake.phase = "ChampSelect"
        fake.cs_session = _cs_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=888)
        acct = FakeAcct()
        svc = _make_svc(fake, acct=acct)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 5), "选人 session 未建立"
            # 先等首轮加载完成（5 名玩家全部请求过）再开始统计，消除"加载中窗口"竞态
            assert _wait(lambda: acct.calls >= 5), "首轮 5 人加载未完成"
            n_calls = acct.calls
            # 同 game_id 的选人持续 tick（多秒）→ session 不被清空、不重复请求
            time.sleep(0.8)
            assert svc.get_session() is not None, "同局选人 tick 误清空 session（churn bug）"
            assert acct.calls == n_calls, "同局选人 tick 误触发重复拉取（churn bug）"
        finally:
            svc.stop()

    def test_champ_select_new_game_id_clears_old_session_once(self):
        """换局（cs gameId 与现有 session 不同）→ 清空一次，随后按新局重建。

        同局选人期间 cs 瞬态缺失（gameId 取不到）不当作换局证据 → 不清空（无 churn）。
        """
        fake = FakeLcu()
        fake.phase = "ChampSelect"
        fake.cs_session = _cs_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=111)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 111), "第一局选人 session 未建立"
            # 同局选人期间 cs 瞬态缺失 → 保留现有 session（修复 churn）
            fake.cs_session = None
            time.sleep(0.4)
            assert svc.get_session() is not None, "同局 cs 瞬态缺失被误清空（churn bug）"
            # 确属换局：cs gameId 变化 → 清空旧局，再按新局重建
            fake.cs_session = _cs_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=222)
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 222), "第二局选人 session 未重建"
        finally:
            svc.stop()


# ---------- 根因 C：进游戏身份升级（summonerId 0→真实）触发重建+补名 ----------

class TestIdentityUpgrade:
    def test_same_puuid_summoner_upgrade_rebuilds_and_fills_names(self):
        """选人 5 puuid（summonerId=0）→ 进游戏 gameflow 同 5 puuid 但 summonerId 真实
        → 触发重建 + 重查身份（by-summonerId）→ 名字/等级不再为空（修复 "? Lv.?"）。"""
        fake = FakeLcu()
        fake.phase = "ChampSelect"
        fake.cs_session = _cs_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=888)
        acct = FakeAcct()
        svc = _make_svc(fake, acct=acct)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 5), "选人 session 未建立"
            # 选人阶段：cs cell summonerId=0 + by-puuid 404 → 全部空名
            pre = svc.get_session()
            assert all(not p.display_name for p in pre.players), "选人阶段应全为空名"

            # 进游戏：同 5 puuid、真实 summonerId（身份升级）
            fake.phase = "InProgress"
            fake.gf_session = _gf_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=888)
            assert _wait(lambda: sum(1 for p in svc.get_session().players if p.summoner_id) == 5), \
                "身份升级未触发重建（summonerId 未变为真实值）"
            # 重建后补名：by-summonerId 拿到名字
            assert _wait(lambda: all(p.display_name for p in svc.get_session().players)), \
                "身份升级后名字未回填（? Lv.? bug）"
            post = svc.get_session()
            # 等级在 history 快照（PlayerSnapshot.summoner_level）上
            assert all(p.history is not None and p.history.summoner_level == 33
                       for p in post.players), "等级未回填"
            # history 保留（重建未打断已加载数据）
            assert all(p.history is not None for p in post.players), "重建后 history 丢失"
        finally:
            svc.stop()

    def test_no_puuid_surge_no_identity_upgrade_no_reload(self):
        """进游戏同 puuid 数且 summonerId 也一致 → 不重建不重拉（回归：同局稳定）。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _gf_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=888, sid_base=300)
        acct = FakeAcct()
        svc = _make_svc(fake, acct=acct)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 5), "session 未建立"
            n_calls = acct.calls
            time.sleep(0.4)
            assert acct.calls == n_calls, "无升级却触发重复拉取"
        finally:
            svc.stop()


# ---------- loader：NO_DATA 空名 = 缺失身份，计入重试且不复用 ----------

def _snap(puuid, state, display_name=""):
    from app.lol.ongoing.models import PlayerSnapshot
    s = PlayerSnapshot(puuid=puuid)
    s.data_state = state
    s.display_name = display_name
    s.loaded_at = time.time()
    return s


class TestLoaderNameMissing:
    def test_no_data_empty_name_counts_as_missing(self):
        """NO_DATA 且空名 → 计入 missing_names_count（触发 retry 补名）。"""
        cache = SnapshotCache(ttl_seconds=300)
        cache.put(_snap("p1", NO_DATA, display_name=""))
        cache.put(_snap("p2", NO_DATA, display_name="有名#tag"))
        l = OngoingGameLoader(lcu_get=lambda ep: None, cache=cache)
        assert l.missing_names_count() == 1  # 只有 p1（空名）

    def test_no_data_with_name_not_missing(self):
        cache = SnapshotCache(ttl_seconds=300)
        cache.put(_snap("p1", NO_DATA, display_name="有名#tag"))
        l = OngoingGameLoader(lcu_get=lambda ep: None, cache=cache)
        assert l.missing_names_count() == 0

    def test_empty_name_not_reusable_any_state(self):
        """空名快照（AVAILABLE/NO_DATA 皆然）不当作缓存复用 → 重新请求补名。"""
        events = []

        def lcu_get(ep):
            if "/lol-summoner/v2/summoners/by-puuid/" in ep or "/lol-summoner/v1/summoners/by-puuid/" in ep:
                return None
            if "/lol-summoner/v2/summoners/" in ep or "/lol-summoner/v1/summoners/" in ep:
                return {"displayName": "补名#tag", "gameName": "补名", "tagLine": "tag",
                        "summonerLevel": 30, "summonerId": 9}
            return None

        cache = SnapshotCache(ttl_seconds=300)
        cache.put(_snap("p1", NO_DATA, display_name=""))       # NO_DATA 空名
        cache.put(_snap("p2", AVAILABLE, display_name=""))     # AVAILABLE 空名
        l = OngoingGameLoader(lcu_get=lcu_get, cache=cache,
                              on_event=lambda c, d: events.append((c, d)))

        # loader 期望 champ-select 兼容格式（myTeam/theirTeam）
        snaps = l.load_for_champ_select(
            {"gameId": 1, "myTeam": [
                {"puuid": "p1", "summonerId": 9, "championId": 1, "cellId": 0},
                {"puuid": "p2", "summonerId": 9, "championId": 2, "cellId": 1},
            ], "theirTeam": []})
        names = {s.puuid: s.display_name for s in snaps}
        assert names.get("p1") == "补名#tag", "NO_DATA 空名未重查补名"
        assert names.get("p2") == "补名#tag", "AVAILABLE 空名未重查补名"


# ---------- 诊断事件：cs 分队反解明细 ----------

class TestCsTeamResolveEvent:
    def test_cs_team_resolve_emitted(self):
        fake = FakeLcu()
        fake.phase = "ChampSelect"
        fake.cs_session = _cs_session_5(["m0", "q1", "q2", "q3", "q4"], game_id=888)
        events = []
        svc = OngoingGameService(
            lcu=fake,
            on_event=lambda c, d: events.append((c, d)),
            account_history_service=FakeAcct(),
            snapshot_provider=None,
            gsm_provider=None,
            deobfuscate_enabled=True,
            poll_interval=0.05, rps=100, max_workers=2,
        )
        svc.start()
        try:
            assert _wait(lambda: any(c == "ongoing.cs_team_resolve" for c, _ in events))
            ev = next(d for c, d in events if c == "ongoing.cs_team_resolve")
            assert ev["my"]["total"] == 5
            assert ev["their"]["total"] == 0
        finally:
            svc.stop()
