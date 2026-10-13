"""修仙称号 12+5 档判定测试（纯计算，无 UI 依赖）。

覆盖：12 档（有段位）正例 / 5 档（无段位）正例 / 辅助阈值分位置 /
      降档（含 IRON 边界）/ 10 场窗口门槛 / 胜率边界。
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


# ---- 档 1/2：10 场窗口（<5 场不评，回落 20 场档） ----

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
    """10 场窗口 <5 场：不评档 1/2，回落到 20 场档（大圆满）。"""
    r = _t(games_10=4, win_rate_10=0.80, average_kda_10=4.5, streak_10=5,
           win_rate=0.60, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·大圆满"  # 20 场档 5


# ---- 档 3-12：20 场窗口（有段位） ----

def test_tier3_daoxin():
    """尽力局局长：胜率低但 KDA 高 → 道心破碎。"""
    r = _t(win_rate=0.40, average_kda=3.5)
    assert r is not None
    assert r.text == "翡翠境·道心破碎"
    assert r.color == COLORS[3]


def test_tier4_dieluo_drops_tier():
    """翡翠降 1 大段 → 铂金境·跌落。"""
    r = _t(win_rate=0.35, average_kda=1.0, streak=-4)
    assert r is not None
    assert r.text == "铂金境·跌落"
    assert r.color == COLORS[4]


def test_tier4_iron_no_drop():
    """黑铁降无可降 → 保持黑铁境·跌落。"""
    r = _t(win_rate=0.35, average_kda=1.0, streak=-4, tier="IRON")
    assert r is not None
    assert r.text == "黑铁境·跌落"


def test_tier5_dayuanman():
    r = _t(win_rate=0.60, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·大圆满"
    assert r.color == COLORS[5]


def test_tier6_yuanman():
    r = _t(win_rate=0.50, average_kda=2.5)
    assert r is not None
    assert r.text == "翡翠境·圆满"
    assert r.color == COLORS[6]


def test_tier7_banbu():
    r = _t(win_rate=0.40, average_kda=1.6)  # C 位 1.6 = mid_low [1.5,2.0)
    assert r is not None
    assert r.text == "半步翡翠境"
    assert r.color == COLORS[7]


def test_tier8_fuyuan():
    """胜率高但 KDA 中低/低 → 福缘（原 7 档落空区间，现已覆盖）。"""
    r = _t(win_rate=0.60, average_kda=1.0)
    assert r is not None
    assert r.text == "翡翠境·福缘"
    assert r.color == COLORS[8]


def test_tier9_gaogao():
    """中胜率 + KDA 高/中高 → 孤高。"""
    r = _t(win_rate=0.50, average_kda=3.5)
    assert r is not None
    assert r.text == "翡翠境·孤高"
    assert r.color == COLORS[9]


def test_tier10_zhongyong():
    """中胜率 + KDA 中低/低 → 中庸。"""
    r = _t(win_rate=0.50, average_kda=1.0)
    assert r is not None
    assert r.text == "翡翠境·中庸"
    assert r.color == COLORS[10]


def test_tier11_shiwei():
    """低胜率 35~45% + KDA 中/低 → 式微。"""
    r = _t(win_rate=0.40, average_kda=2.0)  # C 位 2.0 = mid [2.0,3.0)
    assert r is not None
    assert r.text == "翡翠境·式微"
    assert r.color == COLORS[11]


def test_tier12_shuaibai():
    """胜率<35% + KDA 中/中低/低 → 衰败。"""
    r = _t(win_rate=0.30, average_kda=1.6)
    assert r is not None
    assert r.text == "翡翠境·衰败"
    assert r.color == COLORS[12]


# ---- 无段位 5 档（tier=None，纯称号无"境"前缀，20 场窗口） ----

def test_no_tier_zaohua():
    """胜率≥60% + KDA 高 → 造化（金）。"""
    r = _t(tier=None, win_rate=0.65, average_kda=4.5)
    assert r is not None
    assert r.text == "造化"
    assert r.color == COLORS[1]


def test_no_tier_jingjin():
    """胜率≥55% 但 KDA 非高 → 精进（蓝）。"""
    r = _t(tier=None, win_rate=0.60, average_kda=1.0)
    assert r is not None
    assert r.text == "精进"
    assert r.color == COLORS[5]


def test_no_tier_shouchang():
    r = _t(tier=None, win_rate=0.50)
    assert r is not None
    assert r.text == "守常"
    assert r.color == COLORS[6]


def test_no_tier_kundun():
    r = _t(tier=None, win_rate=0.40)
    assert r is not None
    assert r.text == "困顿"
    assert r.color == COLORS[3]


def test_no_tier_chenlun():
    r = _t(tier=None, win_rate=0.30)
    assert r is not None
    assert r.text == "沉沦"
    assert r.color == COLORS[12]


def test_no_tier_wr060_kda_low_is_jingjin():
    """胜率 0.60 + KDA 低：造化不中（KDA 非高）→ 精进。"""
    r = _t(tier=None, win_rate=0.60, average_kda=1.0)
    assert r.text == "精进"


# ---- 辅助阈值分位置 ----

def test_support_threshold():
    """UTILITY 占比 >50% → 辅助阈值：kda 1.6 = 中 → 档 6 圆满。"""
    r = _t(win_rate=0.50, average_kda=1.6, utility_ratio=0.60)
    assert r is not None
    assert r.text == "翡翠境·圆满"


def test_carry_threshold():
    """C 位阈值：kda 1.6 = 中低 → 45~55% 胜率 + 中低 → 档 10 中庸（12 档全覆盖不再落空）。"""
    r = _t(win_rate=0.50, average_kda=1.6, utility_ratio=0.20)
    assert r is not None
    assert r.text == "翡翠境·中庸"


# ---- 边界 / 兜底 ----

def test_wr_boundary_055():
    """0.55 进档 5 大圆满，0.54 落档 6 圆满（半开区间）。"""
    r = _t(win_rate=0.55, average_kda=2.5)
    assert r.text == "翡翠境·大圆满"
    r2 = _t(win_rate=0.54, average_kda=2.5)
    assert r2.text == "翡翠境·圆满"


def test_always_has_title_with_tier():
    """有段位时 12 档全覆盖：任意 win_rate/KDA 组合都有称号（不再落空）。"""
    for wr in (0.9, 0.6, 0.5, 0.4, 0.3, 0.2):
        for kda in (0.5, 1.0, 1.4, 1.8, 2.5, 4.0):
            r = _t(win_rate=wr, average_kda=kda)
            assert r is not None, f"wr={wr} kda={kda} 应有称号"
