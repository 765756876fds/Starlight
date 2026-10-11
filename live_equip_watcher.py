"""
实时装备监控 - 只报敌方：成装 + 饰品眼 + 辅助装升级
排除：自己、小件、药水、饼干
"""
import sys
sys.path.insert(0, '.')

import requests
import urllib3
import time
urllib3.disable_warnings()

from app.speech.tts import TTSController
from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_B

base = "https://127.0.0.1:2999"

# 完全排除的物品（药水、饼干、消耗品）
EXCLUDE_ITEMS = {
    "药水", "饼干", "药剂", "血瓶", "蓝瓶", "复用型药水",
    "生命药水", "法力药水", "控制守卫", "真眼",
}

# 散件/小件 完全排除
COMPONENTS = {
    "长剑", "十字镐", "红水晶", "布甲", "抗魔斗篷", "草鞋", "匕首", "短剑",
    "蓝水晶", "增幅", "仙女护符", "治疗宝珠", "黑暗封印",
    "多兰", "小冰心", "燃烧宝石", "负极斗篷", "熔渣",
    "短匕", "反曲之弓", "暴风之剑", "无用大棒", "巨人腰带",
    "幽魂斗篷", "萃取",
    "狂热", "蜂刺", "黄叉", "以太精魂", "恶魔法典",
    "耀光", "冰川护甲", "守望者铠甲", "远古盾牌",
    "比尔吉沃特弯刀", "小圣杯", "和谐圣杯",
    "提亚马特", "班德尔玻璃镜", "遗失的章节",
    "裂句", "锯齿短匕", "考尔菲德的战锤",
}

# 饰品眼（只报这个）
EYE_ITEMS = {"侦察守卫", "神谕透镜", "扫描透镜", "监视图腾"}

# 辅助装（报形态升级）
SUPPORT_ITEMS = {
    "焰爪猫", "风行狐", "星界据守", "月石再生器",
    "阴影", "守望之眼", "骑士之誓", "基克",
}

def is_tracked(item_name, team):
    # 只报敌方（CHAOS）
    if team != "CHAOS":
        return False
    if not item_name:
        return False
    # 排除药水饼干
    for ex in EXCLUDE_ITEMS:
        if ex in item_name:
            return False
    # 排除小件
    for comp in COMPONENTS:
        if comp in item_name:
            return False
    # 饰品眼 要报
    for eye in EYE_ITEMS:
        if eye in item_name:
            return True
    # 辅助装 要报
    for sup in SUPPORT_ITEMS:
        if sup in item_name:
            return True
    # 其他所有都是成装，要报
    return True

print("初始化 TTS...")
tts = TTSController({})
tts.start()
sq = SpeechQueue(tts)
sq.start()

print("开始监控：只报敌方的成装 + 饰品眼 + 辅助装升级...")
last_items = {}

try:
    while True:
        try:
            r = requests.get(f"{base}/liveclientdata/playerlist", verify=False, timeout=2)
            if r.status_code == 200:
                players = r.json()
                for p in players:
                    name = p.get("summonerName", "未知")
                    champ = p.get("championName", name)
                    team = p.get("team", "")
                    items = p.get("items", [])
                    current = set()
                    for item in items:
                        item_name = item.get("displayName", "")
                        if is_tracked(item_name, team):
                            current.add(item_name)
                    
                    if not current:
                        continue
                    
                    if name not in last_items:
                        last_items[name] = current
                        print(f"[INIT] 敌方 {champ}: {current}")
                        for item in sorted(current):
                            text = f"敌方{champ}有{item}。"
                            print(f"[VOICE] {text}")
                            sq.enqueue(SpeechItem(
                                text=text,
                                priority=PRIORITY_B,
                                expire_at=time.time() + 10,
                                category="equipment",
                                event_id=f"init_{hash(text) % 10000}"
                            ))
                            time.sleep(0.5)
                        continue
                    
                    new = current - last_items[name]
                    if new:
                        for item in sorted(new):
                            text = f"敌方{champ}更新了{item}。"
                            print(f"[VOICE] {text}")
                            sq.enqueue(SpeechItem(
                                text=text,
                                priority=PRIORITY_B,
                                expire_at=time.time() + 10,
                                category="equipment",
                                event_id=f"new_{hash(text) % 10000}"
                            ))
                            time.sleep(0.5)
                    last_items[name] = current
            time.sleep(1)
        except Exception as e:
            time.sleep(2)
            print(f"等待游戏连接...")
except KeyboardInterrupt:
    pass

print("监控停止")
sq.shutdown()
tts.stop()
