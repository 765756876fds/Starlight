"""LoL 静态数据：英雄 / 技能 / 召唤师技能目录。

本包提供只读的本地 catalog，数据来源：
- 召唤师技能 CD：硬编码静态表（极少变动）。
- 英雄 / 技能 CD：内置少量测试英雄（亚索/艾希/拉克丝），
  可通过 updater 从 CommunityDragon / Meraki 拉取后覆盖到 data/lol_data/。
"""
from .ability_catalog import Ability, AbilityCatalog
from .champion_catalog import Champion, ChampionCatalog
from .summoner_spell_catalog import SummonerSpell, SummonerSpellCatalog

__all__ = [
    "Ability",
    "AbilityCatalog",
    "Champion",
    "ChampionCatalog",
    "SummonerSpell",
    "SummonerSpellCatalog",
]
