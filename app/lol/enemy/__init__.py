"""敌方观察层：自动资料准备 + 能力边界声明。"""
from .roster import EnemyProfile, EnemyRoster
from .cast_source import (
    EnemyAbilityObservation, CastSupport,
    ability_cast_support, should_auto_track_summoner_spells,
    ABILITY_CAST_SUPPORT, SUMMONER_SPELL_AUTO_TRACK_ENABLED,
)

__all__ = [
    "EnemyProfile", "EnemyRoster",
    "EnemyAbilityObservation", "CastSupport",
    "ability_cast_support", "should_auto_track_summoner_spells",
    "ABILITY_CAST_SUPPORT", "SUMMONER_SPELL_AUTO_TRACK_ENABLED",
]
