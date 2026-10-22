"""选人名字闪烁终极修复：数据源仲裁 + generation 代际保护 定向测试。

覆盖（对齐施工验收 T1/T1b/T3a/T6b/T6c/T7/T9/并发/UI）：
- T1  数据源分裂（gameflow 持续旧局 A、cs 新局 B）：B 合法发布仅一次，不发布 A
- T1b 构建结果局号与目标 B 不一致 → 拒绝发布；数据切回 B 后恢复
- T3a 同局阶段切换（ChampSelect gid B → InProgress gid B）：同局分支重建 + 名字继承
- T6b 换局后 cs 失效：等待零发布（不显示旧局）
- T6c 换局后 gameflow 切新局：自动恢复发布 B，标志清除
- T7  旧代回调/旧代 loader 延迟返回：即使 PUUID 重合也不写入新 session
- T9  新代 loader 正常回填；旧代无法覆盖新代数据
- 并发 换局与旧代写入不穿插（锁内 generation 校验 + 写入）
- UI  等待期 get_session() None（不显示旧局快照）；名字回填后不回退

断言基于 SessionKey / 发布 game_id 与玩家集合 / 异步回填目标 / generation /
UI 最终显示 / 恢复路径，不依赖 DIAG 文本。
"""
import threading as _threading
import time

import pytest

pytestmark = pytest.mark.unit

from app.lol.ongoing.models import PlayerSnapshot
from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE


def _puuid_session(my_puuids, their_puuids, game_id=601006257268):
    """构造 gameflow 响应（按给定 puuid 集合）。"""
    def cell(p, base, cid):
        return {"puuid": p, "summonerId": base, "championId": cid, "cellId": 0}
    t1 = [cell(p, 100 + i, 1 + i) for i, p in enumerate(my_puuids)]
    t2 = [cell(p, 200 + i, 10 + i) for i, p in enumerate(their_puuids)]
    return {"gameId": game_id, "gameData": {"teamOne": t1, "teamTwo": t2}}


def _cs_session(my_puuids, their_puuids, game_id=999):
    """构造 champ-select session（明文 puuid，deobfuscate 直接通过）。

    myTeam 不含 me_puuid（"m0"）时：带有效新局号可触发换局确认，但 deobfuscate
    返回 None → 确认换局后进入等待态（用于 T1b/T6b/T6c/UI 的"确认但未发布"场景）。
    """
    def cell(p, cid):
        return {"puuid": p, "summonerId": 0, "championId": cid,
                "cellId": 0, "nameVisibilityType": "HIDDEN"}
    my = [cell(p, 1 + i) for i, p in enumerate(my_puuids)]
    their = [cell(p, 10 + i) for i, p in enumerate(their_puuids)]
    return {"gameId": game_id, "myTeam": my, "theirTeam": their}


class FakeLcu:
    """模拟 LCU：phase / cs session / gameflow session 可变。"""
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


def _make_svc(fake, acct=None, events=None):
    acct = acct or FakeAcct()
    return OngoingGameService(
        lcu=fake,
        on_event=(lambda c, d: events.append((c, d))) if events is not None else None,
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


# ---------- T1 数据源分裂仲裁 ----------

class TestSourceArbitration:
    def test_t1_stale_gameflow_rejected_cs_published_once(self):
        """gameflow 持续返回旧局 A、cs 返回新局 B：
        换局确认 → 清空旧局 ×1 → 用 cs 构建发布 B（仅一次），不发布 A；标志随后清除。"""
        fake = FakeLcu()
        events = []
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=1000)
        svc = _make_svc(fake, events=events)
        svc.start()
        try:
            # 旧局 A 建立
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            assert len(svc.get_session().players) == 10
            # 切新选人：gameflow 仍返回旧局 A，cs 返回新局 B
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)  # 旧局残留
            fake.cs_session = _cs_session(["m0", "b1", "b2", "b3", "b4"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            # 新局 B 发布
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "新局 B 未发布"
            b = svc.get_session()
            assert len(b.players) == 10, "B 局玩家集合应为 10 人"
            b_puuids = {p.puuid for p in b.players}
            assert b_puuids == {"m0", "b1", "b2", "b3", "b4",
                                "c1", "c2", "c3", "c4", "c5"}, "B 局玩家集合不符"
            # 发布后标志清除（同一套实际构建结果校验）
            assert svc._gf_stale is False, "陈旧标志未在发布后清除"
            assert svc._pending_new_gid == 0, "待切换局号未清零"
            # SessionKey 稳定为新局
            assert svc._current_session_key == "gid:2000"
            # session_created 对 B 仅一次；换局后旧局 A 不再被发布
            created = [d for c, d in events if c == "ongoing.session_created"]
            created_b = [d for d in created if d.get("game_id") == 2000]
            n_a_at_switch = len([d for d in created if d.get("game_id") == 1000])
            assert len(created_b) == 1, "B 局 session_created 应恰好一次"
            # 持续 tick 稳定：B 不反复重建；A 不再出现（根除每 tick 重建旧局空名 session）
            time.sleep(0.3)
            created_after = [d for c, d in events if c == "ongoing.session_created"]
            assert len([d for d in created_after if d.get("game_id") == 2000]) == 1, \
                "B 局被反复发布（闪烁循环未斩断）"
            assert len([d for d in created_after if d.get("game_id") == 1000]) == n_a_at_switch, \
                "换局后旧局 A 被再次发布"
        finally:
            svc.stop()

    def test_t1b_mismatched_build_rejected_then_recovers(self):
        """换局确认锁定目标 B 后，构建结果局号与目标不一致 → 拒绝发布（保持等待）；
        数据切回 B 后恢复发布。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=1000)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            # 确认换局到 B：cs 带新局号但 myTeam 不含 me → 锁定目标 B、清空旧局、构建失败 → 等待
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)
            fake.cs_session = _cs_session(["z1", "z2", "z3", "z4", "z5"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc._gf_stale is True and svc._pending_new_gid == 2000
                         and svc.get_session() is None), "换局目标 B 未锁定/旧局未清空"
            # cs 变为另一局号 C（≠ 目标 B，但可构建）→ 构建结果与目标不一致 → 拒绝发布
            fake.cs_session = _cs_session(["m0", "x1", "x2", "x3", "x4"],
                                          ["y1", "y2", "y3", "y4", "y5"],
                                          game_id=3000)
            assert _wait(lambda: svc.get_session() is None), "局号不匹配时未拒绝发布"
            assert svc._gf_stale is True, "拒绝发布期间陈旧标志应保持"
            assert svc._pending_new_gid == 2000, "目标局号应保持 B（不被 C 覆盖）"
            # 数据切回 B（含 me，可构建）→ 恢复发布
            fake.cs_session = _cs_session(["m0", "b1", "b2", "b3", "b4"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "切回 B 后未恢复发布"
            assert svc._gf_stale is False
            assert svc._pending_new_gid == 0
        finally:
            svc.stop()


# ---------- T6b/T6c 换局后数据不可用：等待不发布 + 恢复 ----------

class TestStaleWaitAndRecover:
    def test_t6b_cs_down_waits_zero_publish(self):
        """换局确认后 cs 失效、gameflow 仍旧局：等待态零发布，UI 不显示旧局。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=1000)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            # 确认换局到 B（cs 带新局号但 myTeam 不含 me → 构建失败，进入等待态）
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)
            fake.cs_session = _cs_session(["z1", "z2", "z3", "z4", "z5"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc._gf_stale is True and svc.get_session() is None), \
                "换局确认后旧局未清空/标志未置位"
            # cs 失效 + gameflow 仍旧局：持续等待，零发布（不发布旧局 A、不发布任何局）
            fake.cs_session = None
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)
            time.sleep(0.4)
            assert svc.get_session() is None, "cs 失效期间不得发布任何 session（含旧局）"
            assert svc._gf_stale is True, "等待期陈旧标志不得清除（无超时/次数放行）"
            assert svc._pending_new_gid == 2000
        finally:
            svc.stop()

    def test_t6c_gameflow_switches_recovers(self):
        """换局后双源暂不可用 → gameflow 切到新局号后自动恢复发布 B（不永久卡死）。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=1000)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            # 确认换局到 B（cs 带新局号但 myTeam 不含 me → 构建失败，进入等待态）
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)
            fake.cs_session = _cs_session(["z1", "z2", "z3", "z4", "z5"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc._gf_stale is True and svc.get_session() is None)
            # cs 失效 + gameflow 仍旧局 → 等待
            fake.cs_session = None
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)
            time.sleep(0.3)
            assert svc.get_session() is None, "双源未就绪期间不得发布"
            # gameflow 切到新局号 B → 自动恢复
            fake.gf_session = _puuid_session(["m0", "b1", "b2", "b3", "b4"],
                                             ["c1", "c2", "c3", "c4", "c5"],
                                             game_id=2000)
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "gameflow 切新局后未恢复发布"
            assert svc._gf_stale is False, "恢复发布后标志未清除"
            assert svc._pending_new_gid == 0
        finally:
            svc.stop()


# ---------- T3a 同局阶段切换（回归） ----------

class TestSameGamePhaseSwitch:
    def test_t3a_champ_select_to_ingame_inherits_names(self):
        """ChampSelect gid B（5 puuid）→ InProgress gid B（10 人）：
        同局分支重建（key 不变），名字/历史继承，不触发跨局守卫。"""
        fake = FakeLcu()
        events = []
        fake.phase = "ChampSelect"
        fake.gf_session = None
        fake.cs_session = _cs_session(["m0", "m1", "m2", "m3", "m4"],
                                      ["e0", "e1", "e2", "e3", "e4"],
                                      game_id=2000)
        svc = _make_svc(fake, events=events)
        svc.start()
        try:
            # 选人阶段 session（5 明文 puuid；敌方 deobfuscate 失败留空 → 5 puuid）
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "选人 session 未建立"
            # 等己方名字由 loader 回填（FakeLcu summoner → "P"）
            assert _wait(lambda: sum(1 for p in svc.get_session().my_team
                                     if p.display_name) == 5), "选人阶段名字未回填"
            names_pre = {p.puuid: p.display_name for p in svc.get_session().my_team}
            key_pre = svc._current_session_key
            # 进游戏：gameflow 同局号 gid 2000，10 人明文
            fake.phase = "InProgress"
            fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                             ["e0", "e1", "e2", "e3", "e4"],
                                             game_id=2000)
            assert _wait(lambda: svc.get_session() is not None
                         and sum(1 for p in svc.get_session().players if p.puuid) == 10), \
                "进游戏未触发同局身份升级重建"
            s = svc.get_session()
            assert s.game_id == 2000
            assert svc._current_session_key == key_pre == "gid:2000", "同局 key 不应变化"
            # 同局继承：己方 5 人名字保留
            for p in s.my_team:
                if p.puuid in names_pre:
                    assert p.display_name == names_pre[p.puuid], \
                        f"同局切换名字未继承: {p.puuid}"
            # 未触发换局标志
            assert svc._gf_stale is False
            assert svc._pending_new_gid == 0
        finally:
            svc.stop()


# ---------- T7/T9 代际保护 ----------

class TestGenerationGuard:
    def test_t7_stale_name_callback_not_written(self):
        """旧代名字回调（换局前捕获的 generation）绝不写入新 session（PUUID 重合也不写）；
        新代回调正常写入。确定性 direct 语义测试（不依赖 loader 异步时序）。"""
        from app.lol.ongoing.session import CurrentGameSession
        svc = OngoingGameService(lcu=None)  # 不启动线程
        # 构造已发布的新局 session（代 gen=1），m0 名字尚未回填
        cs = _cs_session(["m0", "b1", "b2", "b3", "b4"],
                         ["c1", "c2", "c3", "c4", "c5"],
                         game_id=2000)
        rebuilt = svc._to_current_session(cs, "ChampSelect", "m0")
        svc._session = rebuilt
        svc._current_session_key = "gid:2000"
        svc._generation = 1
        # 旧代回调（gen=0，换局前捕获）：必须被丢弃，m0 名字保持空
        svc._on_player_name_resolved("m0", "OldName", generation=0)
        p0 = next(p for p in svc._session.players if p.puuid == "m0")
        assert p0.display_name == "", "旧代名字回调污染了新 session（PUUID 重合也被丢弃）"
        # 新代回调（gen=1，当前代）：正常写入
        svc._on_player_name_resolved("m0", "NewName", generation=1)
        p0 = next(p for p in svc._session.players if p.puuid == "m0")
        assert p0.display_name == "NewName", "新代名字回调未生效"
        # 不带 generation 的旧式回调（None 兼容）：不校验代际但只补空名（回归既有行为）
        svc._on_player_name_resolved("m0", "NoGenName", generation=None)
        p0 = next(p for p in svc._session.players if p.puuid == "m0")
        assert p0.display_name == "NewName", "旧式回调覆盖了已填名字（行为回归）"

    def test_t7_stale_loader_batch_discarded(self):
        """旧代 loader 批量结果（延迟返回）：校验与写入同锁，换局后整体丢弃，不污染新 session。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                         ["e0", "e1", "e2", "e3", "e4"],
                                         game_id=1000)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            assert _wait(lambda: svc._loading_thread_gen is None), "A 局加载未结束"
            gen_a = svc._generation
            release = _threading.Event()
            called = _threading.Event()

            def fake_load(session, force=False, generation=None):
                called.set()
                release.wait(timeout=3)
                snap = PlayerSnapshot(puuid="m0")
                snap.data_state = "AVAILABLE"
                snap.display_name = "OldName"
                return [snap]

            svc._loader.load_for_champ_select = fake_load
            t = _threading.Thread(
                target=lambda: svc._load_in_thread({"gameId": 1000, "myTeam": [], "theirTeam": []}))
            t.start()
            assert called.wait(timeout=2), "旧代 loader 未被调用"
            # 换局：代际推进（等价真实 tick 换局：清空 +1、发布新 session +1）
            svc._generation = gen_a + 1
            svc._session = None
            release.set()
            t.join(timeout=3)
            # 旧代结果被丢弃：当前（空）session 无 OldName 快照；snapshots 未被旧代覆盖
            assert not t.is_alive(), "旧代线程未结束"
            for p in (svc._session.players if svc._session else []):
                assert getattr(p.history, "display_name", None) != "OldName"
            assert all(getattr(s, "display_name", None) != "OldName"
                       for s in svc._snapshots), "旧代 snaps 污染了 snapshots"
        finally:
            svc.stop()

    def test_t9_new_gen_fill_stable_no_regress(self):
        """新代 loader 正常回填；名字出现后连续 tick 不回退成 ?。"""
        fake = FakeLcu()
        fake.phase = "ChampSelect"
        fake.gf_session = None
        fake.cs_session = _cs_session(["m0", "b1", "b2", "b3", "b4"],
                                      ["c1", "c2", "c3", "c4", "c5"],
                                      game_id=2000)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "B 局未发布"
            # 名字回填（loader → summoner "P"）
            assert _wait(lambda: sum(1 for p in svc.get_session().players
                                     if p.display_name) == 10), "名字未回填"
            names_ok = sum(1 for p in svc.get_session().players if p.display_name)
            # 连续 tick 后名字不回退（同局稳定，无重建清空）
            time.sleep(0.4)
            s = svc.get_session()
            names_after = sum(1 for p in s.players if p.display_name)
            assert names_after >= names_ok, "名字回填后回退（UI 将显示 ?）"
            assert svc._current_session_key == "gid:2000"
            assert svc._gf_stale is False
        finally:
            svc.stop()


# ---------- 旧 loader finally 状态竞争（风险 1）确定性并发测试 ----------

class TestGenerationFinallyRace:
    """多代（A→B→C）loader 线程登记与状态所有权：确定性并发测试。

    同步机制（不依赖固定 sleep）：
    - 每代 loader 用独立 Event 门控暂停在结束前（gates[gen]），释放顺序即完成顺序；
    - 每代线程完全结束（含 _load_in_thread 内部 finally）用独立 Event 信号
      （finished[gen]），由包装层在真实 finally 之后置位；
    - 所有 Event.wait(timeout=3) 返回值必须为 True，超时即断言失败（明确失败路径，
      不做假通过、不继续执行到假结论）。

    断言直接检查最终状态与回填结果：登记/loading/force 的所有权、旧代回填丢弃、
    最新代名字回填、状态归位。
    """

    def _run_multi_gen_race(self, newest_first: bool):
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                         ["e0", "e1", "e2", "e3", "e4"],
                                         game_id=1000)
        svc = _make_svc(fake)
        gates = {}      # gen -> Event（loader 暂停门；释放顺序 = 完成顺序）
        finished = {}   # gen -> Event（线程完全结束，含 _load_in_thread 内部 finally）
        entered = set()  # 已进入 loader 的 gen 集合（set.add 原子）
        real_load = svc._load_in_thread

        def gated(session, force=False, generation=None):
            g = generation
            entered.add(g)
            gate = gates.setdefault(g, _threading.Event())
            # 完成信号键随进入预创建：wait 时键必然存在，返回 False 才是真正超时
            finished.setdefault(g, _threading.Event())
            # 门控未释放则阻塞；超时返回 False → 断言失败（明确失败路径）
            assert gate.wait(timeout=3), f"代际 {g} loader 未获释放（超时）"
            tag = f"Gen{g}"
            snaps = []
            for cell in (session.get("myTeam") or []) + (session.get("theirTeam") or []):
                pu = cell.get("puuid")
                if not pu:
                    continue
                snap = PlayerSnapshot(puuid=pu)
                snap.data_state = "AVAILABLE"
                snap.display_name = tag
                snaps.append(snap)
            return snaps

        def wrapped_load(session, gen=None):
            try:
                real_load(session, gen=gen)
            finally:
                # 线程完全结束（内部 finally 已执行完）→ 发完成信号
                finished.setdefault(gen, _threading.Event()).set()

        svc._loader.load_for_champ_select = gated
        svc._load_in_thread = wrapped_load
        svc.start()
        try:
            # ---- A 局建立并进入 A loader（暂停在 gate[gen_a]）----
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            assert _wait(lambda: len(entered) >= 1), "A 局 loader 未进入"
            gen_a = min(entered)
            # ---- 换局 B（cs 2000）→ B loader 进入（暂停在 gate[gen_b]）----
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                             ["e0", "e1", "e2", "e3", "e4"],
                                             game_id=1000)
            fake.cs_session = _cs_session(["m0", "b1", "b2", "b3", "b4"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "B 局未发布"
            assert _wait(lambda: len(entered) >= 2), "B 局 loader 未启动（被旧代登记误挡）"
            # ---- 换局 C（cs 3000）→ C loader 进入（暂停在 gate[gen_c]）----
            fake.cs_session = _cs_session(["m0", "c1", "c2", "c3", "c4"],
                                          ["d1", "d2", "d3", "d4", "d5"],
                                          game_id=3000)
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 3000), "C 局未发布"
            assert _wait(lambda: len(entered) >= 3), "C 局 loader 未启动（被旧代登记误挡）"
            gen_c = max(entered)
            gen_b = next(g for g in entered if g != gen_a and g != gen_c)
            assert gen_a < gen_b < gen_c, "代际应随换局单调递增"
            # ---- 三线程并存：登记 / loading / force 均属于最新代 C ----
            assert svc._loading_thread_gen == gen_c, "线程登记应属于最新代 C"
            assert svc._loading_in_progress is True, "C 局 loading 应置位"
            assert svc._force_reload is True, "C 局新对局 force 应置位"
            if newest_first:
                # 顺序 1：C（最新代）先完成归位，A、B 迟到 finally
                gates[gen_c].set()
                assert finished[gen_c].wait(timeout=3), f"C(gen{gen_c}) 线程未在超时前结束"
                assert svc._loading_thread_gen is None, "C 完成后登记未归位"
                assert svc._loading_in_progress is False, "C 完成后 loading 未归位"
                assert svc._force_reload is False, "C 完成后 force 未归位"
                gates[gen_a].set()
                gates[gen_b].set()
                assert finished[gen_a].wait(timeout=3), f"A(gen{gen_a}) 线程未在超时前结束"
                assert finished[gen_b].wait(timeout=3), f"B(gen{gen_b}) 线程未在超时前结束"
                # 迟到旧代不得扰动已归位状态
                assert svc._loading_thread_gen is None, "旧代迟到 finally 残留了登记"
                assert svc._loading_in_progress is False, "旧代迟到 finally 重置了 loading"
                assert svc._force_reload is False, "旧代迟到 finally 重置了 force"
            else:
                # 顺序 2：A、B（旧代）先 finally，C 在跑——中途 C 状态不得被清
                gates[gen_a].set()
                gates[gen_b].set()
                assert finished[gen_a].wait(timeout=3), f"A(gen{gen_a}) 线程未在超时前结束"
                assert finished[gen_b].wait(timeout=3), f"B(gen{gen_b}) 线程未在超时前结束"
                assert svc._loading_thread_gen == gen_c, "旧代 finally 清除了 C 的登记"
                assert svc._loading_in_progress is True, "旧代 finally 清除了 C 的 loading"
                assert svc._force_reload is True, "旧代 finally 清除了 C 的 force"
                gates[gen_c].set()
                assert finished[gen_c].wait(timeout=3), f"C(gen{gen_c}) 线程未在超时前结束"
                assert svc._loading_thread_gen is None, "C 完成后登记未归位"
                assert svc._loading_in_progress is False, "C 完成后 loading 未归位"
                assert svc._force_reload is False, "C 完成后 force 未归位"
            # ---- 最终状态：C 局 session、名字只来自 C ----
            s = svc.get_session()
            assert s.game_id == 3000, "最终 session 应为 C 局"
            tag_c = f"Gen{gen_c}"
            assert sum(1 for p in s.players if p.display_name == tag_c) == 10, \
                "C 局名字未回填完整"
            assert not any(p.display_name and p.display_name.startswith("Gen")
                           and p.display_name != tag_c for p in s.players), "旧代回填残留"
        finally:
            for g in gates:
                gates[g].set()
            for g in finished:
                finished[g].set()
            svc.stop()

    def test_newest_thread_finishes_first(self):
        """顺序 1：C（最新代）先归位，A、B 迟到 finally 不扰动已归位状态、回填丢弃。"""
        self._run_multi_gen_race(newest_first=True)

    def test_old_threads_finish_first(self):
        """顺序 2：A、B（旧代）先 finally，C 在跑——不得清除 C 的登记/loading/force。"""
        self._run_multi_gen_race(newest_first=False)

    def test_thread_start_failure_rolls_back_and_recovers(self, monkeypatch):
        """Thread.start 抛异常：登记与同代 loading/force 一并回滚，后续 tick 可重试加载。

        修复前：登记残留且 _loading_in_progress=True 无人复位 → 后续 tick 在
        "elif self._loading_in_progress: return" 永久卡死，名字永不回填。
        修复后：回滚 → 下一 tick retry 重新启动 → 名字回填、状态归位。
        """
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                         ["e0", "e1", "e2", "e3", "e4"],
                                         game_id=1000)
        svc = _make_svc(fake)
        real_start = _threading.Thread.start
        state = {"fail": True}

        def flaky_start(self_, *a, **kw):
            # 仅拦截 service 的加载线程（lcu-load）；diag/主线程正常启动
            if state["fail"] and getattr(self_, "name", "") == "lcu-load":
                state["fail"] = False
                raise RuntimeError("simulated thread start failure")
            return real_start(self_, *a, **kw)

        monkeypatch.setattr(_threading.Thread, "start", flaky_start)
        svc.start()
        try:
            # 局发布成功（_loading_in_progress=True 已置位），但首次 Thread.start 失败
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "局未发布"
            # 登记与同代 loading/force 必须回滚（否则永久卡死）
            assert _wait(lambda: svc._loading_thread_gen is None), \
                "start 失败后线程登记未回滚"
            assert _wait(lambda: svc._loading_in_progress is False), \
                "start 失败后 loading 未回滚（将永久卡死加载）"
            assert svc._force_reload is False
            # 解除故障：后续 tick 的 retry（名字缺失）能重新启动加载并正常回填
            assert _wait(lambda: sum(1 for p in svc.get_session().players
                                     if p.display_name) == 10), "start 恢复后名字未回填"
            assert svc._loading_thread_gen is None, "加载完成后登记未归位"
            assert svc._loading_in_progress is False, "加载完成后 loading 未归位"
            assert svc._force_reload is False, "加载完成后 force 未归位"
        finally:
            state["fail"] = False
            svc.stop()

    def test_a_stalled_after_registration_before_start(self, monkeypatch):
        """时序：A 登记 _loading_thread_gen 后、Thread.start 前暂停 → 换局 B 登记并
        正常启动 → A 才尝试 start（正常路径）。

        验证：A 迟到启动不能清除 B 的登记、复位 B 的 _loading_in_progress /
        _force_reload、阻塞 B 的后续加载；A 的迟到回填被锁内代际校验整体丢弃。
        （A start 抛异常路径：except 守卫 `if self._loading_thread_gen == _spawn_gen`
        与 finally 守卫是同一所有权检查——登记已被 B 接管（gen_B ≠ gen_A）时不成立、
        不动 B；且该 except 在真实代码中只可能发生在 A 自身 tick 内（登记→start→
        except 同 tick 连续执行，B 的登记不可能插入其间），其基础回滚形态已由
        test_thread_start_failure_rolls_back_and_recovers 覆盖。）
        """
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                         ["e0", "e1", "e2", "e3", "e4"],
                                         game_id=1000)
        svc = _make_svc(fake)
        real_start = _threading.Thread.start
        state = {"hold_a": True, "a_thread": None}
        gates = {}       # gen -> Event（loader 暂停门）
        finished = {}    # gen -> Event（线程完全结束）
        entered = set()  # 已进入 loader 的 gen 集合
        real_load = svc._load_in_thread

        def gated(session, force=False, generation=None):
            g = generation
            entered.add(g)
            gates.setdefault(g, _threading.Event())
            finished.setdefault(g, _threading.Event())
            assert gates[g].wait(timeout=3), f"代际 {g} loader 未获释放（超时）"
            tag = f"Gen{g}"
            snaps = []
            for cell in (session.get("myTeam") or []) + (session.get("theirTeam") or []):
                pu = cell.get("puuid")
                if not pu:
                    continue
                snap = PlayerSnapshot(puuid=pu)
                snap.data_state = "AVAILABLE"
                snap.display_name = tag
                snaps.append(snap)
            return snaps

        def wrapped_load(session, gen=None):
            try:
                real_load(session, gen=gen)
            finally:
                finished.setdefault(gen, _threading.Event()).set()

        def start_controller(self_, *a, **kw):
            if getattr(self_, "name", "") != "lcu-load":
                return real_start(self_, *a, **kw)
            if state["hold_a"]:
                # 第一次 lcu-load start（A）：service 已完成登记，这里挂起 start
                # ——确定性模拟"A 登记后、start 前暂停"
                state["hold_a"] = False
                state["a_thread"] = self_
                return
            if self_ is state["a_thread"]:
                # A 迟到恢复 start（正常路径；B 已登记并启动）
                return real_start(self_, *a, **kw)
            return real_start(self_, *a, **kw)

        svc._loader.load_for_champ_select = gated
        svc._load_in_thread = wrapped_load
        monkeypatch.setattr(_threading.Thread, "start", start_controller)
        svc.start()
        try:
            # A 局发布；A 登记完成但 start 被挂起
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "A 局未发布"
            assert _wait(lambda: svc._loading_thread_gen is not None), "A 登记未完成"
            gen_a = svc._loading_thread_gen
            assert state["a_thread"] is not None, "A 的 start 未被挂起（时序编排失败）"
            # 换局 B：B 登记并正常启动，B loader 进入 gate[gen_b]
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session(["m0", "m1", "m2", "m3", "m4"],
                                             ["e0", "e1", "e2", "e3", "e4"],
                                             game_id=1000)
            fake.cs_session = _cs_session(["m0", "b1", "b2", "b3", "b4"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 2000), "B 局未发布"
            assert _wait(lambda: len(entered) >= 1), "B loader 未进入"
            gen_b = svc._loading_thread_gen
            assert gen_b != gen_a, "换局后代际应推进"
            assert gen_b == svc._generation, "登记应属于 B"
            assert svc._loading_in_progress is True, "B loading 应置位"
            assert svc._force_reload is True, "B force 应置位"
            # A 迟到恢复 start（正常路径）→ A 线程进入 gate[gen_a]
            state["a_thread"].start()
            assert _wait(lambda: len(entered) >= 2), "A loader 未进入"
            # A start 后、B 仍在跑：A 不得清除 B 的登记/loading/force
            assert svc._loading_thread_gen == gen_b, "A 迟到启动清除了 B 的登记"
            assert svc._loading_in_progress is True, "A 迟到启动复位了 B 的 loading"
            assert svc._force_reload is True, "A 迟到启动复位了 B 的 force"
            # 释放 A：A 迟到回填（GenA）必须被代际校验丢弃，A finally 不动 B 状态
            gates[gen_a].set()
            assert finished[gen_a].wait(timeout=3), f"A(gen{gen_a}) 线程未在超时前结束"
            assert not any(p.display_name and p.display_name.startswith("Gen")
                           and p.display_name != f"Gen{gen_b}" for p in svc.get_session().players), \
                "A 迟到回填污染了 B 局"
            assert svc._loading_thread_gen == gen_b, "A finally 清除了 B 的登记"
            assert svc._loading_in_progress is True, "A finally 复位了 B 的 loading"
            assert svc._force_reload is True, "A finally 复位了 B 的 force"
            # 释放 B：B 正常完成、名字回填、状态归位（不被 A 阻塞）
            gates[gen_b].set()
            assert finished[gen_b].wait(timeout=3), f"B(gen{gen_b}) 线程未在超时前结束"
            s = svc.get_session()
            assert s.game_id == 2000, "最终 session 应为 B 局"
            assert sum(1 for p in s.players if p.display_name == f"Gen{gen_b}") == 10, \
                "B 局名字未回填完整"
            assert svc._loading_thread_gen is None, "登记未归位"
            assert svc._loading_in_progress is False, "loading 未归位"
            assert svc._force_reload is False, "force 未归位"
        finally:
            for g in gates:
                gates[g].set()
            for g in finished:
                finished[g].set()
            state["hold_a"] = False
            svc.stop()


# ---------- UI 层：等待期不显示旧局快照 ----------

class TestUIWaitState:
    def test_wait_state_get_session_none(self):
        """换局等待期：get_session() 返回 None（深拷贝路径），UI 拿不到旧局快照。"""
        fake = FakeLcu()
        fake.phase = "InProgress"
        fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                         [f"e{i}" for i in range(5)],
                                         game_id=1000)
        svc = _make_svc(fake)
        svc.start()
        try:
            assert _wait(lambda: svc.get_session() is not None
                         and svc.get_session().game_id == 1000), "旧局 A 未建立"
            # 确认换局到 B（cs 带新局号但 myTeam 不含 me → 构建失败，进入等待态）
            fake.phase = "ChampSelect"
            fake.gf_session = _puuid_session([f"m{i}" for i in range(5)],
                                             [f"e{i}" for i in range(5)],
                                             game_id=1000)
            fake.cs_session = _cs_session(["z1", "z2", "z3", "z4", "z5"],
                                          ["c1", "c2", "c3", "c4", "c5"],
                                          game_id=2000)
            assert _wait(lambda: svc._gf_stale is True and svc.get_session() is None)
            # cs 失效后等待期：UI 读取永远拿不到旧局快照
            fake.cs_session = None
            time.sleep(0.3)
            assert svc.get_session() is None, "等待期不应显示旧局快照"
            assert svc.get_snapshots() == [], "等待期不应显示旧局 snapshots"
        finally:
            svc.stop()
