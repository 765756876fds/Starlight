"""OverlayWindow 增量刷新单测（防"每帧全量重建"卡死回归）。

场景覆盖：
1. 同数据两次 _refresh → 控件复用（无全量重建）
2. is_dead 变化 → 增量更新头像（不重建）
3. remaining 变化 → 增量更新技能图标（不重建）
4. scale 变化 → 恰好一次全量重建
5. 从空到有 → 全量重建
6. 从有到空 → 卡片清空
7. hide 期间卡片保留、show 后复用
"""
import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.overlay.overlay_window import OverlayWindow  # noqa: E402
from app.ui.overlay.overlay_model import OverlayEnemyRow, OverlaySpell  # noqa: E402


def _spell(slot: int, sid: int, name: str, remaining: float = -1.0, active: bool = False) -> OverlaySpell:
    return OverlaySpell(slot=slot, spell_id=sid, spell_name=name,
                        base_cooldown=300.0, remaining=remaining, active=active)


def _row(cid: int = 1, name: str = "亚索", is_dead: bool = False, respawn: float = 0.0,
         r1: float = -1.0, a1: bool = False, r2: float = -1.0, a2: bool = False) -> OverlayEnemyRow:
    return OverlayEnemyRow(
        champion_id=cid, champion_name=name, position="MIDDLE", order=0,
        spell1=_spell(1, 4, "闪现", r1, a1),
        spell2=_spell(2, 7, "治疗", r2, a2),
        is_dead=is_dead, respawn_timer=respawn,
    )


def _rows(n: int = 5) -> list:
    names = ["亚索", "永恩", "劫", "泰隆", "阿卡丽"]
    return [_row(i + 1, names[i], r1=float(i)) for i in range(n)]


class _FakeModel:
    def __init__(self, rows):
        self._rows = rows

    def set_rows(self, rows):
        self._rows = rows

    def get_rows(self):
        return self._rows


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _cards_ids(win) -> set:
    return {id(c) for c in win._cards}


@pytest.fixture()
def win(qapp):
    m = _FakeModel([])
    w = OverlayWindow(m)
    yield w, m
    w._timer.stop()
    w._scale_check.stop()
    w.deleteLater()


# 1. 同数据两次 _refresh → 控件复用（无全量重建）
def test_same_rows_no_rebuild(win):
    w, m = win
    m.set_rows(_rows(5))
    w._refresh()
    assert len(w._cards) == 5
    first_ids = _cards_ids(w)

    w._refresh()
    assert _cards_ids(w) == first_ids  # 引用不变 = 无重建


# 2. is_dead 变化 → 增量更新头像（不重建）
def test_death_change_incremental(win):
    w, m = win
    rows = _rows(5)
    m.set_rows(rows)
    w._refresh()
    first_ids = _cards_ids(w)

    rows[2].is_dead = True
    rows[2].respawn_timer = 12.0
    w._refresh()

    assert _cards_ids(w) == first_ids  # 无重建
    assert w._cards[2].avatar._is_dead is True
    assert w._cards[2].avatar._respawn_timer == 12.0
    # 其他卡不受影响
    assert w._cards[0].avatar._is_dead is False


# 3. remaining 变化 → 增量更新技能图标（不重建）
def test_remaining_change_incremental(win):
    w, m = win
    rows = _rows(5)
    m.set_rows(rows)
    w._refresh()
    first_ids = _cards_ids(w)

    rows[0].spell1.remaining = 4.0
    rows[0].spell1.active = True
    w._refresh()

    assert _cards_ids(w) == first_ids
    assert w._cards[0].spell1_icon._spell.remaining == 4.0
    assert w._cards[0].spell1_icon._spell.active is True


# 4. scale 变化 → 恰好一次全量重建
def test_scale_change_rebuild_once(win):
    w, m = win
    m.set_rows(_rows(5))
    w._refresh()
    old_ids = _cards_ids(w)

    w.set_scale(1.2)      # 只改几何；重建由下一帧 _refresh 的结构指纹触发
    w._refresh()
    new_ids = _cards_ids(w)
    assert new_ids != old_ids  # 重建一次（新尺寸）

    w._refresh()
    assert _cards_ids(w) == new_ids  # 再刷新：复用，不再重建


# 5. 从空到有 → 全量重建
def test_empty_to_rows(win):
    w, m = win
    assert w._cards == []
    m.set_rows(_rows(5))
    w._refresh()
    assert len(w._cards) == 5


# 6. 从有到空 → 卡片清空
def test_rows_to_empty(win):
    w, m = win
    m.set_rows(_rows(5))
    w._refresh()
    assert len(w._cards) == 5

    m.set_rows([])
    w._refresh()
    assert w._cards == []
    assert not w.isVisible()


# 7. hide 期间卡片保留、show 后复用
def test_hide_keeps_cards(win):
    w, m = win
    m.set_rows(_rows(5))
    w._refresh()
    assert w.isVisible()
    first_ids = _cards_ids(w)

    w.hide()              # 直接隐藏窗口（非数据清空）
    assert w._cards       # 卡片保留

    w.show()
    w._refresh()          # show 后同数据：复用
    assert _cards_ids(w) == first_ids
