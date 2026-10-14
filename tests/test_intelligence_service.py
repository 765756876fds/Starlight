"""intelligence_service 失败节流 + 缓存行为测试（A+C 修复）。"""
from unittest.mock import MagicMock, patch

from app.lol.ongoing.intelligence_service import OngoingIntelligenceService


def _make_service(intelligence_service=None):
    mastery = MagicMock()
    mastery.get_top3.return_value = ("AVAILABLE", [])
    return OngoingIntelligenceService(
        mastery_provider=mastery,
        sgp_provider=None,
        intelligence_service=intelligence_service,
    )


def test_sgp_fail_not_cached():
    """SGP 失败（provider 未建）→ recent_status=UNAVAILABLE 且不写缓存。"""
    svc = _make_service()
    with patch.object(svc, "_ensure_sgp", return_value=None):
        entry = svc.get_player_intelligence("puuid-1")
    assert entry.recent_status == "UNAVAILABLE"
    assert svc.get_cached("puuid-1") is None  # 失败不写缓存 → 可重试


def test_throttle_active_then_expires():
    """失败后立即节流 True；30 秒后放行 False。"""
    svc = _make_service()
    t0 = 1000.0
    with patch.object(svc, "_ensure_sgp", return_value=None), \
         patch("time.monotonic", return_value=t0):
        svc.get_player_intelligence("puuid-2")
    assert svc._last_fail.get("puuid-2") == t0
    with patch("time.monotonic", return_value=t0):
        assert svc.should_throttle("puuid-2") is True  # 刚失败 → 节流
    with patch("time.monotonic", return_value=t0 + 30.0):
        assert svc.should_throttle("puuid-2", window=30.0) is False  # 30s 后放行


def test_success_caches_and_clears_throttle():
    """SGP 成功（有效场 ≥1）→ 写缓存 + 清失败时间戳。"""
    intel = MagicMock()
    intel.analyze.return_value.games = 5  # 有效场 ≥1 → AVAILABLE
    svc = _make_service(intelligence_service=intel)
    # 先失败一次（制造 _last_fail 残留）
    with patch.object(svc, "_ensure_sgp", return_value=None):
        svc.get_player_intelligence("puuid-3")
    assert svc.should_throttle("puuid-3")

    # 再成功：SGP provider 就绪
    prov = MagicMock()
    res = MagicMock()
    res.state = "AVAILABLE"
    res.records = [MagicMock()]
    prov.recent.return_value = res
    svc._sgp = prov
    with patch("time.monotonic", return_value=2000.0):
        entry = svc.get_player_intelligence("puuid-3")
    assert entry.recent_status == "AVAILABLE"
    assert svc.get_cached("puuid-3") is not None  # 成功写缓存
    assert svc.should_throttle("puuid-3") is False  # 失败时间戳已清


def test_zero_valid_games_not_available():
    """训练营/自定义：records 非空但有效场 0（全被 PVE 过滤）
    → recent_status=EMPTY 且不写缓存（不评称号，避免 0 数据落空）。"""
    intel = MagicMock()
    intel.analyze.return_value.games = 0
    svc = _make_service(intelligence_service=intel)
    prov = MagicMock()
    res = MagicMock()
    res.state = "AVAILABLE"
    res.records = [MagicMock()]  # 原始记录非空（全是 PVE/训练营）
    prov.recent.return_value = res
    svc._sgp = prov
    entry = svc.get_player_intelligence("puuid-4")
    assert entry.recent_status == "EMPTY"
    assert svc.get_cached("puuid-4") is None  # EMPTY 不写缓存 → 可重试


def test_is_valid_match_queue_whitelist():
    """队列白名单：只算 匹配(400/430/460/490) + 单双排(420) + 灵活(440)。
    470 双人排位 / 0 自定义 / 880 未知娱乐 / 人机 870 / 大乱斗 450 → 全排除。"""
    from app.lol.ongoing.intelligence import _is_valid_match
    from app.lol.ongoing.match_model import MatchRecord

    def rec(qid):
        return MatchRecord(game_id=1, game_creation=1, queue_id=qid,
                           champion_name="Yasuo", win=True, kills=5, deaths=2, assists=8)

    for qid in (400, 420, 430, 440, 460, 490):
        assert _is_valid_match(rec(qid)) is True, f"queue {qid} 应算有效"
    for qid in (470, 0, 880, 870, 450):
        assert _is_valid_match(rec(qid)) is False, f"queue {qid} 应排除"


def test_is_valid_match_only_queue_whitelist():
    """候选 A：字段缺失不再过滤（中性剔除在统计层），queue 白名单仍生效。"""
    from app.lol.ongoing.intelligence import _is_valid_match
    from app.lol.ongoing.match_model import MatchRecord

    # 白名单内：win=None / KDA 缺失 仍算有效场（统计层中性处理）
    assert _is_valid_match(MatchRecord(queue_id=420, win=None)) is True
    assert _is_valid_match(MatchRecord(queue_id=420, win=True, kills=None, deaths=2, assists=8)) is True
    # 白名单外：仍排除
    assert _is_valid_match(MatchRecord(queue_id=450, win=True, kills=5, deaths=2, assists=8)) is False
