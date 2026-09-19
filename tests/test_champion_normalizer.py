"""英雄别名单测：确定性映射、未知返回 None、不匹配错。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.lol.champion.normalizer import normalize_champion_name, ALIASES

# A canonical passthrough: unknown/non-alias returns None, caller falls back to raw
assert normalize_champion_name("众星之子") is None
# B alias
assert normalize_champion_name("奶妈") == "众星之子"
# C several high-confidence
for a, c in [("风女", "风暴之怒"), ("琴女", "琴瑟仙女"), ("诺手", "诺克萨斯之手"),
             ("女警", "皮城女警"), ("石头人", "熔岩巨兽")]:
    assert normalize_champion_name(a) == c, (a, normalize_champion_name(a))
# D unknown -> None
assert normalize_champion_name("不存在的英雄xyz") is None
assert normalize_champion_name("") is None
assert normalize_champion_name(None) is None
# E no positional alias
assert normalize_champion_name("ADC") is None or normalize_champion_name("ADC") == "ADC"
print("champion aliases:", len(ALIASES))
print("ALL NORMALIZER TESTS PASS")
