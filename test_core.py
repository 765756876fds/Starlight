"""
测试脚本：核心架构 + 意图规则匹配
不需要 AI 模型
"""
import sys
import os
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.core.event_bus import get_event_bus, Event, EventType
from app.core.command_bus import get_command_bus, Command, CommandType
from app.core.state_store import get_state_store, StateNamespace
from app.core.scheduler import get_scheduler
from app.voice.intent import RuleBasedIntent, Intent


def test_event_bus():
    """测试事件总线"""
    print("=" * 50)
    print("测试 1: 事件总线")
    print("=" * 50)

    bus = get_event_bus()
    received = []

    def handler(event: Event):
        received.append(event)
        print(f"  收到事件: {event.event_type} - {event.data}")

    bus.subscribe("test.event", handler)
    bus.publish(Event(event_type="test.event", data={"msg": "hello"}))
    bus.publish(Event(event_type="test.other", data={"msg": "world"}))

    print(f"  接收事件数: {len(received)} (期望 1)")
    assert len(received) == 1, "事件总线测试失败"
    print("  事件总线测试通过！")


def test_command_bus():
    """测试命令总线"""
    print("\n" + "=" * 50)
    print("测试 2: 命令总线")
    print("=" * 50)

    cmd_bus = get_command_bus()
    executed = []

    def handler(cmd: Command):
        executed.append(cmd)
        print(f"  执行命令: {cmd.command_type} - {cmd.params}")
        return "ok"

    cmd_bus.register("test.command", handler)
    result = cmd_bus.execute(Command(command_type="test.command", params={"key": "value"}))

    print(f"  执行结果: {result}")
    assert len(executed) == 1, "命令总线测试失败"
    print("  命令总线测试通过！")


def test_state_store():
    """测试状态存储"""
    print("\n" + "=" * 50)
    print("测试 3: 状态存储")
    print("=" * 50)

    store = get_state_store()
    store.set("test_ns", "key1", "value1")
    store.set("test_ns", "key2", 42)

    val1 = store.get("test_ns", "key1")
    val2 = store.get("test_ns", "key2")
    val3 = store.get("test_ns", "nonexistent", "default")

    print(f"  key1 = {val1}")
    print(f"  key2 = {val2}")
    print(f"  nonexistent = {val3}")

    assert val1 == "value1"
    assert val2 == 42
    assert val3 == "default"
    print("  状态存储测试通过！")


def test_rule_based_intent():
    """测试规则匹配意图识别"""
    print("\n" + "=" * 50)
    print("测试 4: 规则匹配意图识别")
    print("=" * 50)

    test_cases = [
        # 音乐控制
        ("下一首", CommandType.MUSIC_NEXT),
        ("下一曲", CommandType.MUSIC_NEXT),
        ("换一首", CommandType.MUSIC_NEXT),
        ("切歌", CommandType.MUSIC_NEXT),
        ("上一首", CommandType.MUSIC_PREV),
        ("暂停", CommandType.MUSIC_PAUSE),
        ("暂停一下", CommandType.MUSIC_PAUSE),
        ("继续", CommandType.MUSIC_RESUME),
        ("继续播放", CommandType.MUSIC_RESUME),
        ("声音大一点", CommandType.MUSIC_VOLUME_UP),
        ("声音小一点", CommandType.MUSIC_VOLUME_DOWN),
        ("跳高潮", CommandType.MUSIC_HIGHLIGHT),
        ("跳到高潮部分", CommandType.MUSIC_HIGHLIGHT),
        # 搜索播放
        ("播放周杰伦的歌", CommandType.MUSIC_SEARCH_PLAY),
        ("来一首勇敢", CommandType.MUSIC_SEARCH_PLAY),
        # 游戏提醒
        ("对面莎弥拉闪现了", "alarm.flash"),
        ("莎弥拉闪现了", "alarm.flash"),
        ("帮我记一下对面莎弥拉闪现", "alarm.flash"),
        ("莎弥拉 W用了", "alarm.spell"),
        ("这里插了个眼", "alarm.ward"),
        ("两分钟后提醒我", "alarm.custom"),
    ]

    passed = 0
    failed = 0

    for text, expected_intent in test_cases:
        intent = RuleBasedIntent.match(text)
        if intent and intent.intent_type == expected_intent:
            print(f"  ✓ '{text}' -> {intent.intent_type} {intent.params}")
            passed += 1
        else:
            actual = intent.intent_type if intent else "None"
            print(f"  ✗ '{text}' -> 期望 {expected_intent}, 实际 {actual}")
            failed += 1

    print(f"\n  通过: {passed}/{len(test_cases)}, 失败: {failed}")
    assert failed == 0, f"规则匹配测试有 {failed} 个失败"
    print("  规则匹配测试通过！")


def test_scheduler():
    """测试调度器"""
    print("\n" + "=" * 50)
    print("测试 5: 任务调度器")
    print("=" * 50)

    scheduler = get_scheduler()
    counter = {"count": 0}

    def task():
        counter["count"] += 1

    scheduler.add_task("test_task", task, interval=0.2)
    scheduler.start()

    time.sleep(0.5)
    scheduler.stop()

    print(f"  任务执行次数: {counter['count']} (期望 >= 2)")
    assert counter["count"] >= 2, "调度器测试失败"
    print("  调度器测试通过！")


if __name__ == "__main__":
    print("LoL Music Agent - 核心架构测试")
    print()

    try:
        test_event_bus()
        test_command_bus()
        test_state_store()
        test_rule_based_intent()
        test_scheduler()

        print("\n" + "=" * 50)
        print("所有核心架构测试通过！")
        print("=" * 50)

    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
