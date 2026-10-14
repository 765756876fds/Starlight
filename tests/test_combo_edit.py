"""一键连招二期第一步测试：序列化 / 保存写回 / reload 立即生效 / 校验规则。

不依赖真实键盘、真实 UI（对话框不测，逻辑层全覆盖）。
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.combo.config import ComboConfig  # noqa: E402
from app.combo.manager import ComboManager  # noqa: E402
from app.combo.models import Combo, ComboStep  # noqa: E402
from app.combo.validation import validate_combo  # noqa: E402


# ---------- 测试辅助（与 test_combo.py 同构，避免跨文件 import） ----------

class FakeSender:
    def __init__(self):
        self.keys = []

    def __call__(self, key):
        self.keys.append(key)


def make_session(self_champion_id):
    self_player = SimpleNamespace(is_self=True, champion_id=self_champion_id)
    other = SimpleNamespace(is_self=False, champion_id=1)
    return SimpleNamespace(my_team=[self_player, other])


class FakeOngoing:
    def __init__(self, session):
        self._session = session

    def get_session(self):
        return self._session


# ---------- 序列化 / 保存 ----------

def test_serialize_roundtrip(tmp_path):
    """load → to_yaml_dict → dump → 再 load，数据完全一致（不丢英雄/连招）。"""
    path = str(tmp_path / "combos.yaml")
    cfg1 = ComboConfig.load(path)
    data = cfg1.to_yaml_dict()
    assert set(data["champions"]) == {"盲僧", "锤石"}
    dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    (tmp_path / "combos.yaml").write_text(dumped, encoding="utf-8")
    cfg2 = ComboConfig.load(path)
    assert len(cfg2.champions) == 2
    lee1 = cfg1.find_by_id(64)
    lee2 = cfg2.find_by_id(64)
    assert lee2.find_by_trigger("3").summary == lee1.find_by_trigger("3").summary
    assert [s.key for s in lee2.find_by_trigger("a").sequence] == ["4", "d", "w", "w"]
    assert cfg2.find_by_id(412).find_by_trigger("a").summary == "D→W→Q"


def test_save_writes_file_and_preserves_existing(tmp_path):
    """改内存 → save → 重新 load：新连招在、原有英雄/连招保留。"""
    path = str(tmp_path / "combos.yaml")
    cfg = ComboConfig.load(path)
    lee = cfg.find_by_id(64)
    lee.combos.append(Combo(trigger="s", note="新增测试连招",
                            sequence=[ComboStep("e", 50)]))
    assert cfg.save() is True
    cfg2 = ComboConfig.load(path)
    lee2 = cfg2.find_by_id(64)
    assert lee2.find_by_trigger("s").note == "新增测试连招"
    assert [s.key for s in lee2.find_by_trigger("3").sequence] == ["3", "w", "w"]
    assert cfg2.find_by_id(412).find_by_trigger("a").summary == "D→W→Q"
    # 文件顶部注释说明存在
    head = (tmp_path / "combos.yaml").read_text(encoding="utf-8").splitlines()[0]
    assert "此文件由 UI 保存时会重写" in head


def test_save_no_data_loss_all_champions_written(tmp_path):
    """保存时全量写回（不只写改动的那个英雄）。"""
    path = str(tmp_path / "combos.yaml")
    cfg = ComboConfig.load(path)
    cfg.find_by_id(64).combos.append(Combo(trigger="s", sequence=[ComboStep("q", 10)]))
    cfg.save()
    cfg2 = ComboConfig.load(path)
    assert set(cfg2.by_id) == {64, 412}  # 锤石没动也在


# ---------- Manager reload / 立即生效 ----------

def make_manager(tmp_path, key_sender=None):
    m = ComboManager(
        config_path=str(tmp_path / "combos.yaml"),
        key_sender=key_sender,
        ongoing_service=FakeOngoing(make_session(64)),
        usage_path=str(tmp_path / "usage.json"),
    )
    return m


def _wait_until(pred, timeout=2.0):
    """轮询等待条件成立（连招执行已移出调用线程，断言前需等待异步完成）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_manager_save_config_reload_effective(tmp_path):
    """保存后无需重启：新连招 trigger 立即能触发。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_window_gate(False)
    m.set_enabled(True)
    # 直接改内存配置（等价于 UI 编辑后保存）
    lee = m.config.find_by_id(64)
    lee.combos.append(Combo(trigger="s", note="新连招", sequence=[ComboStep("e", 50)]))
    assert m.save_config() is True
    # reload 后新配置生效
    m._handle_key("s")
    assert _wait_until(lambda: len(sender.keys) == 1)
    assert _wait_until(lambda: not m._executor.is_executing)  # T1 完全结束（_ignore_until 已设）
    assert sender.keys == ["e"]
    # 原有连招仍在（先过 grace 窗口再按 3）
    m._ignore_until = time.time() - 0.1
    m._handle_key("3")
    assert _wait_until(lambda: len(sender.keys) == 4)
    assert sender.keys[-3:] == ["3", "w", "w"]


def test_manager_add_champion_and_save(tmp_path):
    m = make_manager(tmp_path)
    cc = m.add_champion("亚索", 777, ["yasuo", "yasu"])
    assert cc is not None
    assert cc.champion_id == 777
    assert cc.combos == []  # 新英雄连招为空
    # 文件里真的写进去了
    cfg = ComboConfig.load(str(tmp_path / "combos.yaml"))
    added = cfg.find_by_id(777)
    assert added is not None and added.name == "亚索"
    assert set(cfg.by_id) == {64, 412, 777}  # 原有英雄没丢


def test_manager_add_champion_duplicate_rejected(tmp_path):
    m = make_manager(tmp_path)
    assert m.add_champion("盲僧", 64, []) is None  # 已存在
    assert len(m.config.champions) == 2


# ---------- 触发键捕获解析（TriggerCaptureDialog.normalize_key 纯函数） ----------

def test_capture_normalize_letters_digits():
    from app.ui.dialogs.combo_dialogs import TriggerCaptureDialog
    n = TriggerCaptureDialog.normalize_key
    assert n("a") == "a"
    assert n("A") == "a"  # 大写统一小写
    assert n("Z") == "z"
    assert n("5") == "5"  # 主键盘数字
    assert n("0") == "0"


def test_capture_normalize_rejects_special_keys():
    from app.ui.dialogs.combo_dialogs import TriggerCaptureDialog
    n = TriggerCaptureDialog.normalize_key
    assert n("shift") is None
    assert n("ctrl") is None
    assert n("alt") is None
    assert n("tab") is None
    assert n("esc") is None
    assert n("space") is None
    assert n("f1") is None
    assert n("up") is None
    assert n("num 1") is None  # 小键盘数字（主键区才允许）
    assert n("enter") is None
    assert n("") is None


# ---------- 校验规则 ----------

def test_validate_trigger_duplicate(tmp_path):
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    ok, errors = validate_combo(champ, "3", [ComboStep("q", 50)])
    assert ok is False
    assert any("已被本英雄的另一个连招使用" in e for e in errors)


def test_validate_illegal_trigger_rejected(tmp_path):
    """非字母数字触发键（符号）→ 触发键不合法。QWERDF 现在合法（UI 层有警告）。"""
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    ok, errors = validate_combo(champ, "-", [ComboStep("q", 50)])
    assert ok is False
    assert "触发键不合法" in errors


def test_validate_negative_delay_rejected(tmp_path):
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    ok, errors = validate_combo(champ, "s", [ComboStep("q", -5)])
    assert ok is False
    assert any("≥ 0 的整数" in e for e in errors)


def test_validate_non_int_delay_rejected(tmp_path):
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    bad = SimpleNamespace(key="q", delay_after_ms="abc")
    ok, errors = validate_combo(champ, "s", [bad])
    assert ok is False
    assert any("≥ 0 的整数" in e for e in errors)


def test_validate_empty_sequence_rejected(tmp_path):
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    ok, errors = validate_combo(champ, "s", [])
    assert ok is False
    assert "连招至少需要 1 个按键" in errors


def test_validate_bad_sequence_key_rejected(tmp_path):
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    ok, errors = validate_combo(champ, "s", [SimpleNamespace(key="x", delay_after_ms=10)])
    assert ok is False
    assert any("按键不合法" in e for e in errors)


def test_validate_edit_same_trigger_not_duplicate(tmp_path):
    """编辑模式：trigger 不变（命中自己）不应误报重复。"""
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    existing = champ.find_by_trigger("3")
    ok, errors = validate_combo(champ, "3", existing.sequence, exclude=existing)
    assert ok is True, errors


def test_validate_zero_delay_ok(tmp_path):
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    ok, errors = validate_combo(champ, "s", [ComboStep("w", 0), ComboStep("w", 50)])
    assert ok is True, errors


def test_validate_legal_trigger_keys(tmp_path):
    """触发键合法集：字母 a-z + 数字 0-9（避开盲僧已有 3/4/a 的重复校验）。"""
    champ = ComboConfig.load(str(tmp_path / "combos.yaml")).find_by_id(64)
    for k in "abcdefghijklmnopqrstuvwxyz0123456789":
        if k in ("3", "4", "a"):
            continue  # 已被盲僧占用，测重复场景不在此处
        ok, errors = validate_combo(champ, k, [ComboStep("q", 50)])
        assert ok is True, (k, errors)


# ---------- 二期第二步：删除 ----------

def test_manager_remove_champion_deletes_all_combos(tmp_path):
    """删英雄：连它所有连招一起删，保存后 reload 生效，索引同步清理。"""
    m = make_manager(tmp_path)
    assert m.find_champion_by_id(64) is not None
    assert m.remove_champion(64) is True
    assert m.find_champion_by_id(64) is None            # reload 后消失
    assert m.config.find_by_alias("盲僧") is None       # aliases 索引已清理
    assert 64 not in [c.champion_id for c in m.get_champions()]
    # 重新从磁盘加载确认已写入
    cfg = ComboConfig.load(str(tmp_path / "combos.yaml"))
    assert cfg.find_by_id(64) is None
    assert cfg.find_by_id(412) is not None              # 锤石不受影响


def test_manager_remove_champion_unknown_returns_false(tmp_path):
    m = make_manager(tmp_path)
    assert m.remove_champion(999999) is False


def test_remove_combo_keeps_champion(tmp_path):
    """删连招只删连招、保留英雄（等价 UI 路径：champ.combos.remove + save_config）。"""
    m = make_manager(tmp_path)
    lee = m.find_champion_by_id(64)
    combo = lee.find_by_trigger("3")
    assert combo is not None
    lee.combos.remove(combo)
    assert m.save_config() is True
    # reload 后：英雄在、被删连招没了、其他连招保留
    lee2 = m.find_champion_by_id(64)
    assert lee2 is not None
    assert lee2.find_by_trigger("3") is None
    assert lee2.find_by_trigger("4") is not None
    assert m.find_champion_by_id(412) is not None       # 锤石不受影响


def test_delete_all_champions_empties_library(tmp_path):
    """全删后 get_champions() 为空，空库可正常保存/加载（不崩）。"""
    m = make_manager(tmp_path)
    assert m.remove_champion(64) is True
    assert m.remove_champion(412) is True
    assert m.get_champions() == []
    assert m.config.find_by_id(64) is None
    assert m.config.find_by_id(412) is None
    assert m.save_config() is True
    cfg = ComboConfig.load(str(tmp_path / "combos.yaml"))
    assert cfg.champions == []
