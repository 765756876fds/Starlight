# -*- coding: utf-8 -*-
"""
Core Precision: Intent 正负向 / 歧义边界测试。

只测 RuleBasedIntent.match 的路由结果，不启动真实 ASR/麦克风/事件总线。
核心原则：
- 明确指令稳定命中
- 裸陈述/模糊话不误创建 USER_MEMORY
- 用户记忆查询/取消只落在 alarm.*，不误触发音乐/cooldown
"""
from app.voice.intent import RuleBasedIntent as R


def t(text):
    i = R.match(text)
    return i.intent_type if i else None


CASES = [
    # (输入, 期望意图 or None)
    # 正向：明确记忆动作
    ("对面莎弥拉闪现了，帮我记一下", "alarm.flash"),
    ("帮我记一下莎弥拉闪现", "alarm.flash"),
    ("记一下对面莎弥拉闪现了", "alarm.flash"),
    # 查询/取消（用户记忆）
    ("记了什么", "alarm.list"),
    ("刚才记了什么", "alarm.list"),
    ("还有多久", "alarm.remaining"),
    ("取消刚才那个", "alarm.cancel_last"),
    ("取消刚才那个记录", "alarm.cancel_last"),
    # 音乐
    ("下一首", "music.next"),
    ("暂停音乐", "music.pause"),
    ("继续", "music.resume"),
    # 负向：裸陈述不应自动创建 USER_MEMORY
    ("对面莎弥拉闪现了", None),
    ("莎弥拉闪现了", None),
]

fails = []
for text, expected in CASES:
    got = t(text)
    ok = (got == expected)
    if not ok:
        fails.append((text, expected, got))
    print(("PASS" if ok else "FAIL"), repr(text), "expected=", expected, "got=", got)

# 反向断言：这些输入不得被判成 alarm.flash
MUST_NOT_FLASH = [
    "对面莎弥拉闪现了",          # 裸陈述
    "对面莎弥拉闪现好了",        # 另一套 cooldown，绝不能建 USER_MEMORY
    "还有多久",                  # 查询
    "记了什么",                  # 查询
]
for text in MUST_NOT_FLASH:
    got = t(text)
    if got == "alarm.flash":
        fails.append((text, "not alarm.flash", got))
        print("FAIL must-not-flash", repr(text), got)

print("----")
if fails:
    print("FAILURES:", len(fails))
    raise SystemExit(1)
print("ALL INTENT PRECISION TESTS PASS")
