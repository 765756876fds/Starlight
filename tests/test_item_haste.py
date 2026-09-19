"""item -> Ability Haste 接线 stub。"""
import sys; sys.path.insert(0, ".")
from app.lol.data.item_haste import sum_ability_haste, data_version
from app.lol.cooldown.manager import CooldownManager
from app.lol.cooldown.calculator import CooldownCalculator

print("item_haste version:", data_version())

# table lookup
ah, src = sum_ability_haste([3118, 3100])   # Malignance 15 + Lich Bane 10
print("items [3118,3100] -> ah=%s src=%s" % (ah, src))
assert ah == 25 and src == "live_inventory"

ah0, src0 = sum_ability_haste([1001, 0])
print("no-ah items -> ah=%s src=%s" % (ah0, src0))
assert ah0 == 0 and src0 == "unavailable"

# CooldownCalculator with haste
base = 100.0
cd_no = CooldownCalculator.for_ability(base, "W", ability_haste=0)
cd_ah = CooldownCalculator.for_ability(base, "W", ability_haste=25)
print("CD base=%s, AH25=%s" % (cd_no, cd_ah))
assert abs(cd_no - 100) < 0.1
assert abs(cd_ah - 80) < 0.5   # 100*100/125=80

# CooldownManager haste_provider wired via handle_start
spoken = []
class Cmd:
    params = {"champion": "石头人", "skill": "W", "side": "enemy"}
m = CooldownManager(speech_enqueue=lambda t, e: spoken.append(t),
                    haste_provider=lambda champ, side="enemy": 25.0)
m.set_game_time(0.0)
r = m.handle_start(Cmd())
print("W estimated_cd with AH25 =", r.estimated_cooldown, "base =", r.base_cooldown)
assert r.estimated_cooldown < r.base_cooldown

# no provider -> base
m2 = CooldownManager(speech_enqueue=lambda t, e: None)
m2.set_game_time(0.0)
r2 = m2.handle_start(Cmd())
assert abs(r2.estimated_cooldown - r2.base_cooldown) < 0.01

print("ALL ITEM-HASTE STUB PASS")
import os; os._exit(0)

