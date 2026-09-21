"""A 验收自动对账：读取 app_out.log 与今日 events jsonl，逐项判定。
用法：  python tests/acceptance_check.py
只读，不改任何东西。
"""
import os, re, json, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = os.path.join(ROOT, "data", "app_out.log")
ev = sorted(glob.glob(os.path.join(ROOT, "data", "events", "*.jsonl")),
            key=os.path.getmtime)[-1]

logs = open(out, encoding="utf-8", errors="replace").read() if os.path.exists(out) else ""
events = []
if os.path.exists(ev):
    for line in open(ev, encoding="utf-8", errors="replace"):
        try: events.append(json.loads(line))
        except: pass

def has(pat, src=logs): return re.search(pat, src) is not None

print("== 文件 ==")
print("app_out:", out, "len", len(logs))
print("events :", ev, "n=", len(events))

# 只看本次启动后的（粗略：最后 1500 行）
recent = logs[-60000:]
print("\n== 语音/Intent ==")
print("当前 KDA  意图:", "current_self_kda" in recent)
print("对面最肥  意图:", "current_richest" in recent)
print("经济差    意图:", "economy_diff" in recent)
print("诚实措辞'读不到金币':", "读不到双方金币" in recent)
print("误报'经济接近'(应False):", "双方经济接近" in recent)
print("player.query enqueue:", "PlayerQuery] enqueued" in recent)

print("\n== Cooldown ==")
print("cooldown.started:", has(r"cooldown\.started", recent))
print("remind(大约N秒好):", has(r"大约\d+秒好", recent))
print("cooldown.ready:", has(r"cooldown\.ready", recent))

print("\n== Objective ==")
types = {}
for e in events[-400:]:
    t = e.get("event_type","")
    if any(k in t for k in ("dragon","baron","herald","grub","alarm","objective")):
        types[t] = types.get(t,0)+1
for k,v in sorted(types.items()): print(f"  {k}: {v}")

print("\n== 生命周期 ==")
starts = [e for e in events if e.get("event_type")=="game.started"]
ends   = [e for e in events if e.get("event_type")=="game.ended"]
print("game.started:", len(starts), "game.ended:", len(ends))

print("\n== 音乐 ==")
print("mpv resp:", "mpv] resp" in recent)
print("music.search_play:", "music.search_play" in recent)
print("\nDONE")
