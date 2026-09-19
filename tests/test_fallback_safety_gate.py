# -*- coding: utf-8 -*-
"""Fallback Safety Gate：伪造 LLM 输出，验证 alarm.* 不被放行。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.voice.intent import IntentRouter, Intent


class FakeLLM:
    """伪造的 LLM fallback：永远返回指定 intent。"""
    def __init__(self, intent_type, conf=0.99):
        self._t, self._c = intent_type, conf
    def is_available(self):
        return True
    def parse(self, text):
        return Intent(self._t, confidence=self._c, params={"champion": "莎弥拉"})


def make_router(llm_type, conf=0.99):
    r = IntentRouter({})
    r.llm_intent = None
    r.api_intent = FakeLLM(llm_type, conf)
    return r


fails = []

# Test: LLM -> alarm.flash conf 0.99 仍被拒
r = make_router("alarm.flash", 0.99)
got = r.parse("对面莎弥拉闪现了")
ok = got.intent_type == "system.unknown"
print(("PASS" if ok else "FAIL"), "LLM alarm.flash 0.99 ->", got.intent_type)
if not ok: fails.append("alarm.flash gate")

# LLM -> alarm.spell 拒绝
r = make_router("alarm.spell")
got = r.parse("任意")
ok = got.intent_type == "system.unknown"
print(("PASS" if ok else "FAIL"), "LLM alarm.spell ->", got.intent_type)
if not ok: fails.append("alarm.spell gate")

# LLM -> alarm.custom 拒绝
r = make_router("alarm.custom")
got = r.parse("任意")
ok = got.intent_type == "system.unknown"
print(("PASS" if ok else "FAIL"), "LLM alarm.custom ->", got.intent_type)
if not ok: fails.append("alarm.custom gate")

# LLM -> music.next 允许
r = make_router("music.next")
got = r.parse("胡言乱语xyz")
ok = got.intent_type == "music.next"
print(("PASS" if ok else "FAIL"), "LLM music.next ->", got.intent_type)
if not ok: fails.append("music.next passthrough")

# Rule 明确命中 alarm.flash，不走 LLM（api_intent 是 alarm.flash 伪造，但 Rule 应直接返回）
r = make_router("alarm.flash")
got = r.parse("对面莎弥拉闪现了，帮我记一下")
ok = got.intent_type == "alarm.flash" and got.params.get("champion")
print(("PASS" if ok else "FAIL"), "Rule explicit alarm.flash ->", got.intent_type, got.params)
if not ok: fails.append("rule explicit unaffected")

# 裸陈述走 Rule = None（确认规则层）
from app.voice.intent import RuleBasedIntent as R
ok = R.match("对面莎弥拉闪现了") is None
print(("PASS" if ok else "FAIL"), "rule bare statement -> None")
if not ok: fails.append("rule bare")

print("----")
if fails:
    print("FAILURES:", fails); raise SystemExit(1)
print("ALL FALLBACK GATE TESTS PASS")
