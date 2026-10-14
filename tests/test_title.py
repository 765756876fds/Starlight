"""修仙称号 8 档判定测试（纯计算，无 UI 依赖）。

覆盖：8 档正例（C 位 + 辅助）/ 请神 OR（连胜）/ 崩盘 OR（连败+低KDA）/
      孤高 vs 稳健边界 / 力竭 vs 崩盘边界 / 无段位纯称号 /
      全覆盖穷举 / 辅助阈值分位置。
"""
import pytest

from app.lol.title import compute_title, COLORS


def _t(**kw):
    """默认中位参数（C 位，翡翠段），按需覆盖。"""
    base = dict(
        win_rate=0.50, average_kda=2.5, streak=1,
        utility_ratio=0.2, tier="EMERALD",
    )
    base.update(kw)
    return compute_title(**base)


# ---- a. 8 档正例（C 位） ----

CARRY_CASES = [
    # (档, win_rate, average_kda, streak, 预期文本)
    (1, 0.70, 5.0, 1, "翡翠境·请神"),
    (2, 0.60, 3.9, 1, "翡翠境·天人合一"),
    (3, 0.55, 3.1, 1, "翡翠境·大圆满"),
    (4, 0.44, 3.6, 1, "翡翠境·孤高"),
    (5, 0.46, 2.3, 1, "翡翠境·稳健"),
    (6, 0.40, 1.6, 1, "翡翠境·颓势"),
    (7, 0.33, 1.1, 1, "翡翠境·力竭"),
    (8, 0.31, 0.9, 1, "翡翠境·崩盘"),
]


@pytest.mark.parametrize("level,wr,kda,streak,text", CARRY_CASES)
def test_carry_all_tiers(level, wr, kda, streak, text):
    """C 位 8 档各自命中（从上到下第一命中）。"""
    r = _t(win_rate=wr, average_kda=kda, streak=streak)
    assert r is not None
    assert r.text == text
    assert r.color == COLORS[level]


# ---- a. 8 档正例（辅助） ----

SUPPORT_CASES = [
    (1, 0.70, 4.0, 1, "翡翠境·请神"),    # 辅助 KDA≥3.5
    (2, 0.60, 3.2, 1, "翡翠境·天人合一"),  # 辅助 KDA≥3.0
    (3, 0.55, 2.7, 1, "翡翠境·大圆满"),   # 辅助 KDA≥2.5
    (4, 0.44, 3.0, 1, "翡翠境·孤高"),    # 辅助 KDA≥2.8
    (5, 0.46, 1.9, 1, "翡翠境·稳健"),    # 辅助 KDA≥1.8
    (6, 0.40, 1.3, 1, "翡翠境·颓势"),    # 辅助 KDA≥1.2
    (7, 0.33, 0.9, 1, "翡翠境·力竭"),    # 辅助 KDA≥0.8
    (8, 0.31, 0.7, 1, "翡翠境·崩盘"),
]


@pytest.mark.parametrize("level,wr,kda,streak,text", SUPPORT_CASES)
def test_support_all_tiers(level, wr, kda, streak, text):
    """辅助阈值（utility_ratio>0.5）8 档各自命中。"""
    r = _t(win_rate=wr, average_kda=kda, streak=streak, utility_ratio=0.60)
    assert r is not None
    assert r.text == text
    assert r.color == COLORS[level]


# ---- b. 请神 OR 路径（5+连胜） ----

def test_qingshen_or_streak5():
    """胜率不足 65% 但 5+连胜 + KDA≥3.5/2.8 → 请神。"""
    r = _t(win_rate=0.40, average_kda=3.6, streak=6)
    assert r.text == "翡翠境·请神"
    r_sup = _t(win_rate=0.40, average_kda=2.9, streak=6, utility_ratio=0.60)
    assert r_sup.text == "翡翠境·请神"  # 辅助 KDA≥2.8


# ---- c. 崩盘 OR 路径（5+连败 + 低 KDA） ----

def test_bengpan_or_streak_lose5():
    """5+连败 + KDA<1.5/1.2（且低于档 7 门槛）→ 崩盘。"""
    r = _t(win_rate=0.40, average_kda=0.9, streak=-6)
    assert r.text == "翡翠境·崩盘"
    r_sup = _t(win_rate=0.40, average_kda=0.7, streak=-6, utility_ratio=0.60)
    assert r_sup.text == "翡翠境·崩盘"  # 辅助 KDA<0.8（档 7 门槛下）


# ---- d. 孤高 vs 稳健边界 ----

def test_gugao_vs_wenjian_boundary():
    """KDA 3.6 + wr 0.44 → 孤高；wr 0.46 → 稳健（0.45 边界半开）。"""
    r1 = _t(win_rate=0.44, average_kda=3.6)
    assert r1.text == "翡翠境·孤高"
    r2 = _t(win_rate=0.46, average_kda=3.6)
    assert r2.text == "翡翠境·稳健"


# ---- d2. 力竭 vs 崩盘边界 ----

def test_lijie_vs_bengpan_boundary():
    """5+连败 + KDA 在 [1.0, 1.5) + 胜率≥32% → 力竭（档 7 先于档 8 OR 命中）"""
    r = _t(win_rate=0.35, average_kda=1.2, streak=-6)  # C 位 KDA 1.2 ∈ [1.0, 1.5)
    assert r.text == "翡翠境·力竭"
    # 辅助：KDA 0.9 ∈ [0.8, 1.2)，同样命中力竭
    r_sup = _t(win_rate=0.35, average_kda=0.9, streak=-6, utility_ratio=0.60)
    assert r_sup.text == "翡翠境·力竭"


# ---- e. 无段位（tier=None）→ 纯称号 ----

def test_no_tier_pure_name():
    """无段位：8 档同名，无"X境"前缀。"""
    r1 = _t(tier=None, win_rate=0.70, average_kda=5.0)
    assert r1.text == "请神"
    assert r1.color == COLORS[1]
    r2 = _t(tier=None, win_rate=0.31, average_kda=0.9)
    assert r2.text == "崩盘"
    assert r2.color == COLORS[8]
    r3 = _t(tier=None, win_rate=0.46, average_kda=2.3)
    assert r3.text == "稳健"


# ---- f. 全覆盖穷举 ----

def test_always_has_title():
    """8 档全覆盖：任意 win_rate × KDA 组合都有称号（档 8 兜底）。"""
    for wr in (0.30, 0.50, 0.70):
        for kda in (0.8, 1.5, 2.5, 4.0):
            r = _t(win_rate=wr, average_kda=kda)
            assert r is not None, f"wr={wr} kda={kda} 应有称号"
            assert r.text in ("翡翠境·请神", "翡翠境·天人合一", "翡翠境·大圆满",
                              "翡翠境·孤高", "翡翠境·稳健", "翡翠境·颓势",
                              "翡翠境·力竭", "翡翠境·崩盘")


# ---- g. 辅助阈值分位置 ----

def test_support_vs_carry_threshold():
    """同 KDA 不同位置 → 不同档（阈值分离验证）。"""
    # kda 1.9 + wr 0.46：C 位 <2.2 → 颓势；辅助 ≥1.8 → 稳健
    r_c = _t(win_rate=0.46, average_kda=1.9)
    assert r_c.text == "翡翠境·颓势"
    r_s = _t(win_rate=0.46, average_kda=1.9, utility_ratio=0.60)
    assert r_s.text == "翡翠境·稳健"
    # kda 2.7 + wr 0.55：C 位 <3.0 → 稳健；辅助 ≥2.5 → 大圆满
    r_c2 = _t(win_rate=0.55, average_kda=2.7)
    assert r_c2.text == "翡翠境·稳健"
    r_s2 = _t(win_rate=0.55, average_kda=2.7, utility_ratio=0.60)
    assert r_s2.text == "翡翠境·大圆满"
