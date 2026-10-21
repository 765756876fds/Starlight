"""_zh_* 整理验收：行为零变化对照（期望值 = 改动前快照）。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib

import pytest
pytestmark = pytest.mark.ui



def _mod():
    # 面板模块 import 会拉 PySide6，逐次加载同一模块（缓存避免重复构造）
    return importlib.import_module("app.ui.panels.ongoing_panel")


class TestZhDivision:
    def test_division_unchanged(self):
        """_zh_division 对 I/II/III/IV/小写/空/未知 → 结果与改动前一致（等价 div.upper()）。"""
        m = _mod()
        # 改动前实现：_DIVISION_MAP.get(div.upper(), div)，_DIVISION_MAP 为恒等映射 → 等价 div.upper()
        for div in ["I", "II", "III", "IV", "i", "iv", "", "V", "II ", None]:
            if div is None:
                continue
            assert m._zh_division(div) == div.upper()

    def test_division_map_definition_removed(self):
        """源码层面：_DIVISION_MAP 恒等映射已删除（无定义残留）。"""
        import app.ui.panels.ongoing_panel as m
        assert not hasattr(m, "_DIVISION_MAP")
        with open(m.__file__, encoding="utf-8") as f:
            assert "_DIVISION_MAP = {" not in f.read()


class TestMonsterMap:
    def test_monster_zh_unchanged(self):
        """删重复键后所有野怪映射不变（期望 = 改动前快照）。"""
        m = _mod()
        expected = {
            "DRAGON": "巨龙",
            "ELDER_DRAGON": "远古巨龙",
            "BARON_NASHOR": "纳什男爵",
            "BARON": "纳什男爵",
            "RIFTHERALD": "峡谷先锋",
            "HORDE": "虚空虫群",
            "ATAKHAN": "亚塔坎",
        }
        for k, v in expected.items():
            assert m._monster_zh(k) == v
        # 未知/空兜底不变
        assert m._monster_zh("UNKNOWN_MONSTER") == "UNKNOWN_MONSTER"
        assert m._monster_zh("") == "大型野怪"
        assert m._monster_zh(None) == "大型野怪"


class TestPosMaps:
    def test_pos_zh_unchanged(self):
        """快照位置表 _POS_ZH 键值全量不变（期望 = 改动前快照）。"""
        m = _mod()
        expected = {
            "TOP": "上路", "JUNGLE": "打野", "MIDDLE": "中路",
            "BOTTOM": "下路", "UTILITY": "辅助", "NONE": "", "": "",
        }
        for k, v in expected.items():
            assert m._zh_pos(k) == v
        # strip/upper 兜底不变
        assert m._zh_pos("middle") == "中路"
        assert m._zh_pos(None) == ""

    def test_position_map_unchanged(self):
        """排位卡位置表 _POSITION_MAP 键值全量不变（期望 = 改动前快照）。"""
        m = _mod()
        expected = {
            "TOP": "上单", "JUNGLE": "打野", "MID": "中路",
            "ADC": "下路", "BOT": "下路", "SUPPORT": "辅助",
            "UTILITY": "辅助", "INVALID": "未定",
        }
        for k, v in expected.items():
            assert m._zh_position(k) == v
        assert m._zh_position("top") == "上单"

    def test_two_position_maps_stay_separate(self):
        """两表保持分开且值域不同（TOP 值差异是设计，不是重复）。"""
        m = _mod()
        assert m._POS_ZH["TOP"] == "上路"
        assert m._POSITION_MAP["TOP"] == "上单"
