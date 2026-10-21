"""cooldown runes 契约测试：2999 runes dict → 结构化 list + SSH 启发式。

- SnapshotBuilder._parse_player：2999 runes dict 提取为结构化 list
- ssh_provider.summoner_spell_haste：副系启迪 → 假设星界洞悉 +18 SSH
- _INSPIRATION_TREE_IDS：8300=启迪（8400=坚决，不应触发）
"""
import pytest

pytestmark = pytest.mark.unit

import pytest

from app.lol.cooldown.ssh_provider import (
    _INSPIRATION_TREE_IDS,
    summoner_spell_haste,
)
from app.lol.snapshot import SnapshotBuilder

_RUNES_DICT = {
    "keystone": {"displayName": "余震", "id": 8439, "rawDescription": "x", "rawDisplayName": "y"},
    "primaryRuneTree": {"displayName": "坚决", "id": 8400, "rawDescription": "x", "rawDisplayName": "y"},
    "secondaryRuneTree": {"displayName": "启迪", "id": 8300, "rawDescription": "x", "rawDisplayName": "y"},
}


def _parse_runes(runes_field):
    p = SnapshotBuilder._parse_player({"runes": runes_field}, "")
    return p.runes


def test_runes_dict_to_structured_list():
    """2999 runes dict → 结构化 list，顺序 keystone/primary/secondary"""
    out = _parse_runes(_RUNES_DICT)
    assert out == [
        {"type": "keystone", "id": 8439, "displayName": "余震"},
        {"type": "primary", "id": 8400, "displayName": "坚决"},
        {"type": "secondary", "id": 8300, "displayName": "启迪"},
    ]


def test_runes_empty_returns_list():
    """runes 缺失/空 → [] 不崩"""
    assert _parse_runes(None) == []
    assert _parse_runes({}) == []
    assert _parse_runes([]) == []
    assert _parse_runes("") == []


def test_runes_partial_fields_skipped():
    """子 dict 缺 id 或缺 displayName → 该项跳过，不塞 None"""
    out = _parse_runes({
        "keystone": {"displayName": "余震", "id": 8439},
        "primaryRuneTree": {"displayName": "坚决"},          # 缺 id → 跳过
        "secondaryRuneTree": {"id": 8300},                  # 缺 displayName → 跳过
        "unknown": {"displayName": "x", "id": 1},           # 不在三键 → 忽略
    })
    assert out == [{"type": "keystone", "id": 8439, "displayName": "余震"}]


def test_ssh_inspiration_secondary_plus_18():
    """结构化 runes 副系启迪 → 启发式 +18 SSH"""
    ssh, sources, status = summoner_spell_haste([], [
        {"type": "keystone", "id": 8439, "displayName": "余震"},
        {"type": "primary", "id": 8400, "displayName": "坚决"},
        {"type": "secondary", "id": 8300, "displayName": "启迪"},
    ])
    assert ssh == 18.0
    assert "rune:cosmic_insight_assumed" in sources
    assert status == "ok"


def test_ssh_empty_runes_basic():
    """空 runes → 基础值（0），unavailable"""
    ssh, sources, status = summoner_spell_haste([], [])
    assert ssh == 0.0
    assert sources == []
    assert status == "unavailable"


def test_ssh_not_inspiration_no_bonus():
    """非启迪（主宰/巫术）→ 不加 18"""
    ssh, sources, _ = summoner_spell_haste([], [
        {"type": "keystone", "id": 8229, "displayName": "奥术彗星"},
        {"type": "primary", "id": 8200, "displayName": "巫术"},
        {"type": "secondary", "id": 8100, "displayName": "主宰"},
    ])
    assert ssh == 0.0
    assert sources == []


def test_inspiration_tree_ids_fixed():
    """_INSPIRATION_TREE_IDS 应为 8300（启迪），8400（坚决）不在其中"""
    assert _INSPIRATION_TREE_IDS == {8300}
    assert 8400 not in _INSPIRATION_TREE_IDS
