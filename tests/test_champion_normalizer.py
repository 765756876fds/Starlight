"""英雄名称归一化测试：俗称/本名 → 2999 canonical（称号）。

- ALIASES 目标值与 2999 playerlist.championName（称号）一致。
- 未知称呼返回清洗后的原始名；空值返回 None。
"""
import pytest

pytestmark = pytest.mark.unit

import json
from pathlib import Path

from app.lol.champion.normalizer import ALIASES, normalize_champion_name


def test_nickname_mapping():
    """俗称 → 称号"""
    assert normalize_champion_name("奶妈") == "众星之子"
    assert normalize_champion_name("豹女") == "狂野女猎手"


def test_real_name_mapping():
    """本名 → 称号（champions.json name → title）"""
    assert normalize_champion_name("奈德丽") == "狂野女猎手"
    assert normalize_champion_name("索拉卡") == "众星之子"
    assert normalize_champion_name("烈娜塔") == "炼金男爵"
    assert normalize_champion_name("奥瑞利安") == "铸星龙王"


def test_fixed_legacy_bugs():
    """历史 4 条 value=本名 修正为称号"""
    assert normalize_champion_name("亚索") == "疾风剑豪"
    assert normalize_champion_name("奥巴马") == "圣枪游侠"
    assert normalize_champion_name("牛头") == "牛头酋长"
    assert normalize_champion_name("莎弥拉") == "沙漠玫瑰"


def test_punctuation_stripped():
    """首尾标点兜底"""
    assert normalize_champion_name("奶妈。") == "众星之子"
    assert normalize_champion_name(" 奶妈 ") == "众星之子"
    assert normalize_champion_name("奶妈、") == "众星之子"


def test_unknown_returns_raw():
    """未知称呼返回清洗后的原始名"""
    assert normalize_champion_name("不存在的英雄xyz") == "不存在的英雄xyz"


def test_empty_and_none():
    """空值返回 None"""
    assert normalize_champion_name("") is None
    assert normalize_champion_name(None) is None


def test_no_duplicate_keys():
    """ALIASES 无重复 key"""
    assert len(ALIASES) == len(set(ALIASES.keys()))


def test_all_titles_reflexive():
    """全英雄自反：每个 title 归一化后仍是自己（与 2999 体系一致）"""
    p = Path("data/lol_data/champions.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data, "champions.json 不应为空"
    for c in data:
        t = c.get("title") or ""
        if t:
            assert normalize_champion_name(t) == t, f"title 自反失败: {t!r}"
