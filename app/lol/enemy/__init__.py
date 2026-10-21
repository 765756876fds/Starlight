"""敌方观察层：自动资料准备 + 能力边界声明。"""
from .cast_source import (
    ABILITY_CAST_SUPPORT,
    SUMMONER_SPELL_AUTO_TRACK_ENABLED,
    CastSupport,
    EnemyAbilityObservation,
    ability_cast_support,
    should_auto_track_summoner_spells,
)
from .roster import EnemyProfile, EnemyRoster

__all__ = [
    "ABILITY_CAST_SUPPORT",
    "SUMMONER_SPELL_AUTO_TRACK_ENABLED",
    "CastSupport",
    "EnemyAbilityObservation",
    "EnemyProfile",
    "EnemyRoster",
    "ability_cast_support",
    "should_auto_track_summoner_spells",
]
