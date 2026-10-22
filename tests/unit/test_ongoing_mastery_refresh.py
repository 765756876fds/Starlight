"""绝活判定缓存短路 最小修复 定向测试。

实机根因：UI 首次渲染时 champion_id=0 → _ensure_intel(puuid, 0) 查询 AVAILABLE 战绩并
缓存（current_champion_is_mastery_top3=None，因当前英雄未知）；hero_fill 更新本局英雄
为非零 ID 后，_ensure_intel 命中缓存直接 return，绝活判定永不重算 → UI 恒显"绝活: 未知"。

本修：
1) intelligence_service 缓存条目记录 mastery_judged_champion_id（判定对应的英雄 ID），
   支持"英雄 A→B 变化时不得复用 A 的判定"；
2) get_player_intelligence 缓存命中分支始终按传入 current_champion_id 重算判定（纯内存）；
3) _ensure_intel 缓存命中且判定缺失/对应旧英雄时，允许通过缓存命中路径刷新绝活判定，
   不发起任何网络查询；
4) render_key 纳入绝活判定值，判定 None→True/False 时 UI 能感知并重绘。

断言基于服务缓存条目 / 线程触发行为 / UI 卡片最终文本，不依赖 DIAG 日志文本。
"""
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from app.lol.ongoing.intelligence import PlayerIntelligenceService
from app.lol.ongoing.intelligence_service import OngoingIntelligenceService
from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord
from app.lol.ongoing.champion_mastery import AVAILABLE as MASTERY_AVAILABLE
from app.ui.panels.current_game_panel import CurrentGamePanel


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------- 服务层 fakes ----------

class FakeMastery:
    """按固定 top3 返回的 ChampionMasteryProvider 替身（不碰网络）。"""

    def __init__(self, top3_ids):
        self._top3 = [SimpleNamespace(champion_id=cid) for cid in top3_ids]

    def get_top3(self, puuid):
        return MASTERY_AVAILABLE, self._top3


class FakeSgp:
    def __init__(self, state="AVAILABLE", n_records=1):
        self._state = state
        self._records = [
            MatchRecord(game_id=100 + i, game_creation=1, queue_id=420,
                        champion_name="Yasuo", win=True, kills=5, deaths=2, assists=8)
            for i in range(n_records)
        ] if state == "AVAILABLE" else []

    def recent(self, puuid, start_index=0, count=50):
        return MatchHistoryResult(state=self._state, records=self._records)


def _make_intel(top3_ids=(18, 22, 96), sgp_state="AVAILABLE"):
    return OngoingIntelligenceService(
        mastery_provider=FakeMastery(top3_ids),
        sgp_provider=FakeSgp(sgp_state),
        intelligence_service=PlayerIntelligenceService(),
    )


# ---------- 服务层：缓存命中重算 + 判定英雄绑定 ----------

def test_first_query_champion_zero_keeps_none_and_cached():
    """首次 champion_id=0：战绩 AVAILABLE 写缓存，但绝活判定保持 None、judged=0。"""
    svc = _make_intel()
    entry = svc.get_player_intelligence("p1", 0)
    assert entry.recent_status == "AVAILABLE"
    assert entry.current_champion_is_mastery_top3 is None
    assert entry.mastery_judged_champion_id == 0
    assert svc.get_cached("p1") is entry


def test_cache_hit_recomputes_true_for_top3():
    """缓存命中 + 当前英雄在 top3：判定 True、judged 记录该英雄。"""
    svc = _make_intel(top3_ids=(18, 22, 96))
    svc.get_player_intelligence("p1", 0)          # 首次 0 → None
    entry = svc.get_player_intelligence("p1", 18)  # 缓存命中 → 重算
    assert entry.current_champion_is_mastery_top3 is True
    assert entry.mastery_judged_champion_id == 18


def test_cache_hit_recomputes_false_for_not_top3():
    """缓存命中 + 当前英雄不在 top3：判定 False。"""
    svc = _make_intel(top3_ids=(18, 22, 96))
    svc.get_player_intelligence("p1", 0)
    entry = svc.get_player_intelligence("p1", 145)
    assert entry.current_champion_is_mastery_top3 is False
    assert entry.mastery_judged_champion_id == 145


def test_ab_switch_does_not_reuse_old_judgement():
    """英雄 A→B：缓存里 A 的 True/False 不得复用，必须按 B 重算。"""
    svc = _make_intel(top3_ids=(18, 22, 96))
    svc.get_player_intelligence("p1", 0)
    a = svc.get_player_intelligence("p1", 18)   # A 判定 True
    assert a.current_champion_is_mastery_top3 is True
    b = svc.get_player_intelligence("p1", 145)  # 切到 B
    assert b.current_champion_is_mastery_top3 is False  # 不复用 A
    assert b.mastery_judged_champion_id == 145


def test_repeat_same_champion_judgement_stable():
    """同一英雄重复调用：判定稳定，不产生任何重复查询（缓存命中即返回）。"""
    svc = _make_intel(top3_ids=(18, 22, 96))
    svc.get_player_intelligence("p1", 0)
    e1 = svc.get_player_intelligence("p1", 18)
    before = e1.current_champion_is_mastery_top3, e1.mastery_judged_champion_id
    e2 = svc.get_player_intelligence("p1", 18)
    after = e2.current_champion_is_mastery_top3, e2.mastery_judged_champion_id
    assert before == after == (True, 18)


def test_empty_state_not_cached():
    """SGP EMPTY（无有效排位场次）：不写缓存，get_cached 返回 None（不会触发刷新线程）。"""
    svc = _make_intel(sgp_state="EMPTY")
    entry = svc.get_player_intelligence("p9", 18)
    assert entry.recent_status == "EMPTY"
    assert svc.get_cached("p9") is None


# ---------- UI _ensure_intel：缓存命中后的线程触发行为 ----------

class SyncThread:
    """同步执行 target 的 Thread 替身：确定线程启动行为，不依赖真实调度。"""

    def __init__(self, target, args=(), kwargs=None, daemon=False):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class FakeIntel:
    def __init__(self, cached=None, throttled=False):
        self.cached = cached
        self.throttled = throttled
        self.queries = []

    def should_throttle(self, puuid):
        return self.throttled

    def get_cached(self, puuid):
        return self.cached

    def get_player_intelligence(self, puuid, cid):
        self.queries.append((puuid, cid))
        return self.cached


def _make_panel(fake_intel, monkeypatch):
    monkeypatch.setattr("app.ui.panels.current_game_panel.threading.Thread", SyncThread)
    panel = CurrentGamePanel.__new__(CurrentGamePanel)
    panel._intelligence = fake_intel
    panel._loading_lock = threading.Lock()
    panel._loading_puuids = set()
    panel._fetch_calls = []
    panel._fetch_intel = lambda puuid, cid: panel._fetch_calls.append((puuid, cid))
    return panel


def _entry(mastery_status=MASTERY_AVAILABLE, judged=0, recent="AVAILABLE"):
    return SimpleNamespace(
        mastery_status=mastery_status,
        mastery_judged_champion_id=judged,
        recent_status=recent,
        current_champion_is_mastery_top3=None,
    )


def test_ensure_no_cache_starts_query(monkeypatch):
    """无缓存：保持原查询流程，启动线程。"""
    fake = FakeIntel(cached=None)
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 18)
    assert panel._fetch_calls == [("p1", 18)]


def test_ensure_cached_champion_zero_no_action(monkeypatch):
    """缓存命中 + champion_id=0：保持原行为，不启动线程。"""
    fake = FakeIntel(cached=_entry(judged=0))
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 0)
    assert panel._fetch_calls == []


def test_ensure_cached_non_available_no_action(monkeypatch):
    """缓存命中 + mastery 非 AVAILABLE：不启动线程。"""
    fake = FakeIntel(cached=_entry(mastery_status="UNAVAILABLE", judged=0))
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 18)
    assert panel._fetch_calls == []


def test_ensure_cached_available_mismatch_refreshes(monkeypatch):
    """缓存 AVAILABLE + 判定缺失（judged=0）+ 当前英雄>0：启动线程走缓存命中重算。"""
    fake = FakeIntel(cached=_entry(judged=0))
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 18)
    assert panel._fetch_calls == [("p1", 18)]


def test_ensure_cached_available_same_champion_no_action(monkeypatch):
    """已按当前英雄判定过（judged==cid）：幂等，不重复刷新。"""
    fake = FakeIntel(cached=_entry(judged=18))
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 18)
    assert panel._fetch_calls == []


def test_ensure_a_to_b_refreshes_again(monkeypatch):
    """英雄 A→B：judged=A ≠ B，必须再次刷新（不复用 A 的判定）。"""
    fake = FakeIntel(cached=_entry(judged=18))
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 22)
    assert panel._fetch_calls == [("p1", 22)]


def test_ensure_throttled_no_action(monkeypatch):
    """失败节流期内：不动作。"""
    fake = FakeIntel(cached=None, throttled=True)
    panel = _make_panel(fake, monkeypatch)
    panel._ensure_intel("p1", 18)
    assert panel._fetch_calls == []


# ---------- 集成：render_key 感知 + UI 最终显示 ----------

class FakeOngoing:
    def __init__(self, session):
        self._session = session

    def get_session(self):
        return self._session


class FakeIntelUI:
    """模拟真实异步时序：get_player_intelligence 表示"后台线程已按该英雄执行缓存命中
    重算"（记录判定英雄），判定值生效由测试通过 complete() 显式触发——等价于后台线程
    实际完成、UI 下一次 refresh 才能感知的时刻。"""

    def __init__(self):
        self.entry = SimpleNamespace(
            mastery_status=MASTERY_AVAILABLE,
            mastery_judged_champion_id=0,
            recent_status="AVAILABLE",
            champion_stats=[
                SimpleNamespace(champion_id=18, champion_name="Tristana",
                                win_rate=0.6, games=8),
            ],
            current_champion_is_mastery_top3=None,
            recent_win_rate=0.6, recent_average_kda=3.0, recent_games=8,
            recent_wins=5, recent_losses=3, recent_unknown_count=0,
            streak=0, utility_ratio=0.0,
            games_10=8, win_rate_10=0.6, average_kda_10=3.0, streak_10=0,
        )
        self.query_count = 0

    def should_throttle(self, puuid):
        return False

    def get_cached(self, puuid):
        return self.entry

    def get_player_intelligence(self, puuid, cid):
        # 后台线程调用：缓存命中分支记录"已按该英雄重算"（纯内存）。
        self.query_count += 1
        self.entry.mastery_judged_champion_id = cid
        return self.entry

    def complete(self, cid):
        # 模拟后台线程实际完成：判定原地更新为当前英雄的真实结果。
        self.entry.current_champion_is_mastery_top3 = (cid == 18)


def _player(puuid, cid, name):
    return SimpleNamespace(
        puuid=puuid, display_name=name, champion_id=cid,
        champion_pick_intent=0, live_kills=0, live_deaths=0, live_assists=0,
        live_position="", live_level=0, history=None, realtime_available=False,
        is_self=False, assigned_position="",
    )


def _make_session():
    players = [
        _player("abc123", 18, "玩家A"),  # 当前英雄 18（hero_fill 后）
    ]
    for p in players:
        p.history = SimpleNamespace(data_state="AVAILABLE", recent_matches=[],
                                    summoner_level=30, ranked_tier="GOLD",
                                    ranked_division="II", win_streak=0)
    return SimpleNamespace(
        game_id=9011, phase="InProgress", started_at=time.time() - 120,
        players=players, my_team=players[:], their_team=[],
        has_players=True,
    )


def _make_ui_panel(monkeypatch, intel, session):
    monkeypatch.setattr("app.ui.panels.current_game_panel.threading.Thread", SyncThread)
    panel = CurrentGamePanel(
        snapshot_provider=lambda: SimpleNamespace(game_time=600),
        ongoing_service=FakeOngoing(session),
    )
    panel._intelligence = intel
    return panel


def test_ui_renders_refreshed_mastery(monkeypatch, qapp):
    """集成：判定 None→True 后 render_key 感知变化，UI 重绘并显示"✅ 是绝活"。

    第一次 refresh：缓存判定 None（后台线程尚未完成）→ 卡片"绝活: 未知"。
    模拟后台线程完成缓存原地更新（None→True）后第二次 refresh：
    render_key 变化 → 重绘 → 卡片文本变为"✅ 是绝活"。
    """
    session = _make_session()
    intel = FakeIntelUI()
    panel = _make_ui_panel(monkeypatch, intel, session)

    panel.refresh()
    text_first = panel.ally_cards[0].lbl_current.text()
    assert "绝活: 未知" in text_first
    # 第一次 refresh 期间 _ensure_intel 已触发重算线程（judged 0→18）
    assert intel.entry.mastery_judged_champion_id == 18
    assert intel.query_count == 1

    # 模拟后台线程完成：缓存命中路径原地更新判定为当前英雄（18 在 top3 → True）
    intel.complete(18)
    assert intel.entry.current_champion_is_mastery_top3 is True

    panel.refresh()
    text_after = panel.ally_cards[0].lbl_current.text()
    assert "是绝活" in text_after
    assert "绝活: 未知" not in text_after
    assert intel.query_count == 1  # 判定已按当前英雄生效，第二次 refresh 不重复查询


def test_ui_skips_redraw_when_judgement_unchanged(monkeypatch, qapp):
    """判定已就绪且未变化：render_key 稳定，重复 refresh 不触发无意义重绘。"""
    session = _make_session()
    intel = FakeIntelUI()
    panel = _make_ui_panel(monkeypatch, intel, session)

    panel.refresh()                       # 触发重算线程（judged 0→18），渲染"未知"
    intel.complete(18)                    # 后台完成：判定 True
    panel.refresh()                       # render_key 变化 → 重绘 → "是绝活"
    key1 = panel._last_render_key
    assert "是绝活" in panel.ally_cards[0].lbl_current.text()

    panel.refresh()                       # 无任何变化 → fingerprint 跳过
    assert panel._last_render_key == key1
    assert intel.query_count == 1         # 不因重复 refresh 触发重复查询
