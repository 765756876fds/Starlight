"""修仙称号 7 档判定测试（纯计算，无 UI 依赖）。

覆盖：7 档正例 / 辅助阈值分位置 / 降档（含 IRON 边界）/ 落空 /
      10 场窗口门槛 / 连胜与胜率边界。
"""
import pytest

from app.lol.title import compute_title, COLORS


def _t(**kw):
    """默认 20 场中位参数，按需覆盖。"""
    base = dict(
        win_rate=0.50, average_kda=2.5, streak=1,
        utility_ratio=0.2, tier="EMERALD",
    )
    base.update(kw)
    return compute_title(**base)


# ---- 档 1/2：10 场窗口（<5 场不评） ----

def test_tier1_qingshen():
    r = _t(games_10=6, win_rate_10=0.80, average_kda_10=4.5, streak_10=5)
    assert r is not None
    assert r.text == "翡翠境·请神"
    assert r.color == COLORS[1]


def test_tier2_tianren():
    r = _t(games_10=6, win_rate_10=0.70, average_kda_10=3.5, streak_10=3)
    assert r is not None
    assert r.text == "翡翠境·天人合一"
    assert r.color == COLORS[2]


def test_tier12_not_rated_below_5_games():
    """10 场窗口 <5 场：不评档 1/2，回落到 20 场档。"""
    r = _t(games_10=4, win_rate_10=0.80, average_kda_10=4.5, streak_10=5,
           win_rate=0.60, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·大圆满"  # 20 场档 3


def test_tier1_streak_boundary():
    """连胜 4 不中档 1（需 ≥5）；连胜 2 时档 1/2 都不中 → 落到 20 场档。"""
    r = _t(games_10=6, win_rate_10=0.80, average_kda_10=4.5, streak_10=2,
           win_rate=0.60, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·大圆满"  # 落到 20 场档 3


# ---- 档 3-6：20 场窗口 ----

def test_tier3_dayuanman():
    r = _t(win_rate=0.60, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·大圆满"
    assert r.color == COLORS[3]


def test_tier4_yuanman():
    r = _t(win_rate=0.50, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·圆满"
    assert r.color == COLORS[4]


def test_tier5_banbu():
    r = _t(win_rate=0.40, average_kda=1.7)
    assert r is not None
    assert r.text == "半步翡翠境"
    assert r.color == COLORS[5]


def test_tier6_daoxin():
    """尽力局局长：胜率低但 KDA 高。"""
    r = _t(win_rate=0.40, average_kda=3.5)
    assert r is not None
    assert r.text == "翡翠境·道心破碎"
    assert r.color == COLORS[6]


# ---- 档 7：降档 ----

def test_tier7_dieluo_drops_tier():
    """翡翠降 1 大段 → 铂金境·跌落。"""
    r = _t(win_rate=0.35, average_kda=1.0, streak=-4)
    assert r is not None
    assert r.text == "铂金境·跌落"
    assert r.color == COLORS[7]


def test_tier7_iron_no_drop():
    """黑铁降无可降 → 保持黑铁境·跌落。"""
    r = _t(win_rate=0.35, average_kda=1.0, streak=-4, tier="IRON")
    assert r is not None
    assert r.text == "黑铁境·跌落"


# ---- 辅助阈值分位置 ----

def test_support_threshold():
    """UTILITY 占比 >50% → 辅助阈值：kda 1.6 = 中 → 档 4。"""
    r = _t(win_rate=0.50, average_kda=1.6, utility_ratio=0.60)
    assert r is not None
    assert r.text == "翡翠境·圆满"


def test_carry_threshold():
    """C 位阈值：同 kda 1.6 = 中低 → 档 4 不中（wr 0.5 也不在 35~45%）→ 落空。"""
    r = _t(win_rate=0.50, average_kda=1.6, utility_ratio=0.20)
    assert r is None


# ---- 落空 / 边界 / 兜底 ----

def test_no_title_gap():
    """胜率高但 KDA 极低 → 严格按表落空，返回 None（UI 显示原段位）。"""
    r = _t(win_rate=0.60, average_kda=1.0)
    assert r is None


def test_wr_boundary_055():
    """0.55 进档 3，0.54 落档 4（半开区间）。"""
    r = _t(win_rate=0.55, average_kda=2.5)
    assert r.text == "翡翠境·大圆满"
    r2 = _t(win_rate=0.54, average_kda=2.5)
    assert r2.text == "翡翠境·圆满"


def test_no_tier_returns_none():
    assert _t(tier=None) is None
