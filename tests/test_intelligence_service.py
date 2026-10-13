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
