"""P0 敌方召唤师技能动态 CD：SSH 公式 / provider / 冻结 / 去重。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.lol.cooldown.calculator import CooldownCalculator
from app.lol.cooldown.ssh_provider import summoner_spell_haste

# Test 1: base flash from catalog
from app.lol.data.summoner_spell_catalog import SummonerSpellCatalog
flash = SummonerSpellCatalog().get_by_name("Flash")
assert flash is not None and flash.cooldown == 300.0, flash

# Test 2: SSH formula
assert CooldownCalculator.for_summoner(300, summoner_spell_haste=0) == 300.0
v = CooldownCalculator.for_summoner(300, summoner_spell_haste=18)
assert abs(v - 300*100/118) < 0.1, v

# Test 3: equipment SSH (Ionian boots itemID 3158)
h, src, st = summoner_spell_haste([{"itemID": 3158}], [])
assert h == 12.0 and st == "ok", (h, src)

# Test 4: rune SSH (Cosmic Insight)
h, src, st = summoner_spell_haste([], [{"displayName": "Cosmic Insight"}])
assert h == 18.0 and "rune:" in src[0], (h, src)

# Test 5: unknown / unsupported -> 0 unavailable
h, src, st = summoner_spell_haste([], [])
assert h == 0.0 and st == "unavailable", (h, st)

# Tests 6-9: alarm freeze / dedup / multi-enemy
from app.lol.game_alarm import GameAlarmEngine, AlarmStatus
gae = GameAlarmEngine.__new__(GameAlarmEngine)
gae.config = {}
from app.core.event_bus import get_event_bus
gae.event_bus = get_event_bus()
import threading
gae._lock = threading.Lock()
gae.alarms = {}
gae._current_game_time = 1000.0

a = gae.create_flash_alarm("Samira", cooldown=254.2, source_snapshot={"ssh": 18})
assert a.duration == 254.2 and a.data["source_snapshot"]["ssh"] == 18
# freeze: create does NOT recompute; later change not possible (duration fixed)
assert a.duration == 254.2

# Test 7 duplicate same enemy -> returns same alarm
b = gae.create_flash_alarm("Samira", cooldown=300.0)
assert b is a
assert sum(1 for x in gae.alarms.values() if x.data.get("spell")=="flash") == 1

# Test 8 different enemy -> second timer
c = gae.create_flash_alarm("Jinx", cooldown=300.0)
assert c is not a and sum(1 for x in gae.alarms.values() if x.data.get("spell")=="flash") == 2

# Test 10: ready time independent of TTS (uses fixed duration, no wall clock)
assert abs(a.game_time_end - (1000.0 + 254.2)) < 1e-6

print("ALL P0 SSH TESTS PASS")
