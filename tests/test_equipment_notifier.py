"""敌方成装语音提醒（equipment_notifier v2）测试。

覆盖场景：
- 首帧静默（进游戏不轰炸）
- 首帧记录已有特殊四件（中途启动不重播旧装备）
- 第一件成装（price>2000 非消耗品）播报一次
- 第一件只播一次 + 后续特殊四件走规则 2
- 第一件恰是特殊四件 → 合并 1 条
- 每件只播一次（卖了再买不重播）
- 只播敌方
- 多人同时 → 合并一条（全部播，不截断）
- clear() 重置后重新首帧静默
- 异常防御（非 dict / price 缺失 / 非数字）
"""
import pytest

pytestmark = pytest.mark.unit

from app.lol.equipment_notifier import EquipmentUpdateNotifier
from app.lol.snapshot import PlayerInfo


class FakeSpeechQueue:
    def __init__(self):
        self.queued = []

    def enqueue(self, item):
        self.queued.append(item.text)


def make_player(champion_name, team="CHAOS", items=None, is_current_player=False,
                summoner_name=None):
    """构造 PlayerInfo。默认 team=CHAOS（敌方），无 is_current_player 时
    notifier 兜底敌方=CHAOS。"""
    return PlayerInfo(
        puuid=f"puuid-{champion_name}",
        summoner_name=summoner_name or f"{champion_name}#tag",
        champion_name=champion_name,
        team=team,
        is_current_player=is_current_player,
        items=items or [],
    )


def make_item(display_name, item_id, price):
    return {"displayName": display_name, "itemID": item_id, "price": price,
            "consumable": False, "slot": 0}


# 常用装备
INFINITY_EDGE = make_item("无尽之刃", 3031, 3400)          # 成装 >2000
RANDUIN = make_item("兰顿之兆", 3142, 2700)               # 成装 >2000
ZHONYA = make_item("中娅沙漏", 3157, 2900)                # 特殊四件 >2000
GUARDIAN = make_item("守护天使", 3026, 3200)              # 特殊四件 >2000
SEEKERS = make_item("探索者的护臂", 2420, 1650)           # 特殊四件 <2000
MEJAI = make_item("梅贾的窃魂卷", 3041, 1400)             # 特殊四件 <2000


def test_first_frame_silent_with_existing_items():
    """首帧静默：敌方已有 3 件 >2000 成装 → 不播任何东西"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人", items=[INFINITY_EDGE, RANDUIN, ZHONYA])
    n.update([enemy])
    assert sq.queued == []


def test_first_frame_records_existing_special():
    """首帧记录已有特殊四件：首帧有护臂 → 第二帧不重播护臂"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人", items=[SEEKERS])
    n.update([enemy])          # 首帧静默 + 记录
    n.update([enemy])          # 第二帧：护臂已见，不播
    assert sq.queued == []


def test_first_item_announced_once():
    """第一件成装：敌方第一件出无尽 → 播"对面XX出了无尽之刃" """
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    n.update([enemy])                       # 首帧静默
    enemy.items = [INFINITY_EDGE]
    n.update([enemy])
    assert len(sq.queued) == 1
    assert "对面石头人出了无尽之刃" in sq.queued[0]


def test_first_item_only_once_then_special_via_rule2():
    """第一件只播一次；后续复活甲（特殊四件）走规则 2 额外播"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    n.update([enemy])                       # 首帧静默
    enemy.items = [INFINITY_EDGE]
    n.update([enemy])                       # 播第一件
    assert len(sq.queued) == 1

    enemy.items = [INFINITY_EDGE, GUARDIAN]
    n.update([enemy])                       # 复活甲走规则 2
    assert len(sq.queued) == 2
    assert "对面石头人出了守护天使" in sq.queued[1]
    assert "无尽之刃" not in sq.queued[1]   # 第一件不重播


def test_special_item_below_threshold_via_rule2():
    """特殊四件额外播：护臂 1650 (<2000) 走规则 2 播一次"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    n.update([enemy])                       # 首帧静默
    enemy.items = [SEEKERS]
    n.update([enemy])
    assert len(sq.queued) == 1
    assert "对面石头人出了探索者的护臂" in sq.queued[0]


def test_first_item_is_special_merged():
    """第一件恰是特殊四件 → 合并 1 条（不播两条）"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    n.update([enemy])                       # 首帧静默
    enemy.items = [ZHONYA]
    n.update([enemy])
    assert len(sq.queued) == 1              # 只有 1 条
    assert sq.queued[0].count("中娅沙漏") == 1  # 中娅只出现一次


def test_item_announced_only_once_after_sell_rebuy():
    """每件只播一次：护臂卖了再买 → 不重播"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    n.update([enemy])                       # 首帧静默
    enemy.items = [SEEKERS]
    n.update([enemy])                       # 播护臂
    assert len(sq.queued) == 1

    enemy.items = []                        # 卖了
    n.update([enemy])
    enemy.items = [SEEKERS]                 # 再买
    n.update([enemy])
    assert len(sq.queued) == 1              # 不重播


def test_only_enemy_announced():
    """只播敌方：我方玩家出成装 → 不播；敌方出 → 播"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    me = make_player("盲僧", team="ORDER", is_current_player=True)
    enemy = make_player("魔蛇之拥", team="CHAOS")
    n.update([me, enemy])                   # 首帧静默（含我方 team=ORDER → 敌方=CHAOS）
    me.items = [INFINITY_EDGE]              # 我方出成装
    enemy.items = [RANDUIN]                 # 敌方出成装
    n.update([me, enemy])
    assert len(sq.queued) == 1
    assert "对面魔蛇之拥出了兰顿之兆" in sq.queued[0]
    assert "盲僧" not in sq.queued[0]       # 我方不被播


def test_multi_player_merged_in_one():
    """多人同时 → 合并一条：2 个敌人各出成装 → 一条含两条"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    a = make_player("石头人")
    b = make_player("女枪")
    n.update([a, b])                        # 首帧静默
    a.items = [INFINITY_EDGE]
    b.items = [RANDUIN]
    n.update([a, b])
    assert len(sq.queued) == 1
    assert "对面石头人出了无尽之刃" in sq.queued[0]
    assert "对面女枪出了兰顿之兆" in sq.queued[0]


def test_three_players_not_truncated():
    """Bug 1 验证：一帧内 3 个敌人各出成装 → 3 条都在语音里（不截断）"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    a = make_player("石头人")
    b = make_player("女枪")
    c = make_player("亚索")
    n.update([a, b, c])                     # 首帧静默
    a.items = [INFINITY_EDGE]
    b.items = [RANDUIN]
    c.items = [ZHONYA]
    n.update([a, b, c])
    assert len(sq.queued) == 1
    assert "对面石头人出了无尽之刃" in sq.queued[0]
    assert "对面女枪出了兰顿之兆" in sq.queued[0]
    assert "对面亚索出了中娅沙漏" in sq.queued[0]


def test_clear_resets_first_frame():
    """clear() 重置：clear 后再 update → 又走首帧静默"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    n.update([enemy])                       # 首帧静默
    n.clear()
    enemy.items = [INFINITY_EDGE]
    n.update([enemy])                       # 重新首帧静默（无尽被记录为"第一件已播"）
    assert sq.queued == []
    enemy.items = [INFINITY_EDGE, ZHONYA]   # 帧 2：新增中娅（特殊四件）→ 规则 2 播
    n.update([enemy])
    assert len(sq.queued) == 1
    assert "对面石头人出了中娅沙漏" in sq.queued[0]


def test_exception_safety():
    """异常防御：非 dict item、price 缺失/非数字、字段缺失 → 不崩"""
    sq = FakeSpeechQueue()
    n = EquipmentUpdateNotifier(sq)
    enemy = make_player("石头人")
    enemy.items = [
        "垃圾字符串",                                              # 非 dict
        {"displayName": "无尽之刃", "itemID": 3031, "price": "3400"},  # price 字符串
        {"displayName": "中娅沙漏", "itemID": 3157, "price": None},    # price None（首帧记已见）
        {"displayName": "兰顿之兆", "price": 2700},                    # 缺 itemID
    ]
    n.update([enemy])                       # 首帧静默：中娅记已见、无尽标记 first（不崩）
    n.update([enemy])                       # 帧 2：无新特殊、无新第一件 → 不播（不崩）
    assert sq.queued == []
    enemy.items.append(make_item("梅贾的窃魂卷", 3041, 1400))  # 帧 3：新增特殊四件
    n.update([enemy])                       # 梅贾走规则 2 → 播
    assert len(sq.queued) == 1
    assert "对面石头人出了梅贾的窃魂卷" in sq.queued[0]
