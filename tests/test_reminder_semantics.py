# -*- coding: utf-8 -*-
"""CUSTOM 提醒语义：content 提取、存储、到期播报。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.voice.intent import RuleBasedIntent as R
from app.lol.game_alarm import GameAlarmEngine, AlarmStatus
from app.speech.speech_judge import SpeechJudge
from app.core.event_bus import Event, EventType

fails = []
def chk(n, c):
    print(("PASS" if c else "FAIL"), n)
    if not c: fails.append(n)

# 1. Intent 提取 content
it = R.match("定一个十秒后提醒我喝水")
chk("intent custom", it.intent_type == "alarm.custom")
chk("content=喝水", it.params.get("content") == "喝水")
chk("duration=10", it.params.get("duration") == 10.0)

it2 = R.match("三分钟后提醒我回家")
chk("content=回家", it2.params.get("content") == "回家")
chk("duration=180", it2.params.get("duration") == 180.0)

# 无宾语
it3 = R.match("十秒后提醒我")
chk("无宾语无 content", "content" not in it3.params)

# 2. Alarm 存 content
eng = GameAlarmEngine({}); eng._current_game_time = 100.0
a = eng.create_custom_alarm("十秒提醒", 10.0, lead_time=0.0, content="喝水")
chk("data.content=喝水", (a.data or {}).get("content") == "喝水")
chk("data.source=USER_MEMORY", (a.data or {}).get("source") == "USER_MEMORY")

# 3. SpeechJudge 到期念 content
sj = SpeechJudge({})
alarm_dict = eng._alarm_to_dict(a)
ev = Event(event_type=EventType.ALARM_TRIGGERED, data={"alarm": alarm_dict})
text = sj._generate_speech_text(ev, "B")
chk("到期文本=该喝水了", text == "该喝水了", )

# 4. flash 无 content，回退 title
af = eng.create_flash_alarm("莎弥拉")
evf = Event(event_type=EventType.ALARM_TRIGGERED, data={"alarm": eng._alarm_to_dict(af)})
textf = sj._generate_speech_text(evf, "B")
chk("flash 回退 title=莎弥拉 闪现", textf == "莎弥拉 闪现")

# 5. SYSTEM objective 无 content，回退 title
so = eng.schedule_objective_reminder("小龙刷新", game_time_end=300.0, lead_time=40.0)
evs = Event(event_type=EventType.ALARM_TRIGGERED, data={"alarm": eng._alarm_to_dict(so)})
texts = sj._generate_speech_text(evs, "B")
chk("SYSTEM 回退 title=小龙刷新", texts == "小龙刷新")

# 6. USER_MEMORY 等级不被 sparse DROP
lvl = sj._get_event_level(ev)
chk("custom 到期 level=B（sparse 不降）", lvl == "B")

print("----")
if fails:
    print("FAILURES:", fails); raise SystemExit(1)
print("ALL REMINDER SEMANTICS TESTS PASS")



# 7. content 空 -> 回退 title
b = eng.create_custom_alarm('十秒提醒', 10.0, lead_time=0.0, content='')
evb = Event(event_type=EventType.ALARM_TRIGGERED, data={'alarm': eng._alarm_to_dict(b)})
chk('空content回退title', sj._generate_speech_text(evb,'B') == '十秒提醒')
print('extra ok')
