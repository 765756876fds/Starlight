"""敌方技能释放信号能力声明（能力二：自动识别敌方何时放技能）。

工程结论（基于 2999 实际字段审计，非 README 猜测）：
- eventdata EventName 不含任何 ChampionAbilityCast / SpellCast 事件；
- allPlayers[] 不提供敌方 Q/W/E/R 冷却或施法状态；
- 仅 allPlayers[].summonerSpells[].cooldownRemaining 可观测召唤师技能使用，
  且该自动追踪在 Riot Patch 25.17 政策下属高风险，默认不自动起计时器。

因此：
- 敌方 Ability (Q/W/E/R) 自动释放检测 = NOT_SUPPORTED_BY_2999，不实现；
- 敌方技能冷却计时仍只来自用户语音报告（USER_REPORTED_CAST），走现有 CooldownManager；
- 本模块只做能力声明与（如需）显式观察对象封装，绝不猜测 cast。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CastSupport(str, Enum):
    NOT_SUPPORTED = "not_supported"        # 公开 API 无信号
    OBSERVABLE_GATED = "observable_gated"   # 技术可观测但默认关闭（政策）


# 能力二的权威结论
ABILITY_CAST_SUPPORT = CastSupport.NOT_SUPPORTED
# 召唤师技能使用技术上可由 cooldownRemaining 观测，但默认不自动起计时器
SUMMONER_SPELL_AUTO_TRACK_ENABLED = False
SUMMONER_SPELL_POLICY = "POLICY_RISK_RIOT_25_17"


@dataclass(frozen=True)
class EnemyAbilityObservation:
    """一次已确认的敌方技能释放观察（仅当存在显式可靠信号时构造）。

    不使用概率置信度；只有 verified / unverified。
    当前 2999 不能产生 Q/W/E/R 的 verified 观察，故本类不用于自动路径。
    """
    match_id: str
    summoner_name: str
    champion_name: str
    ability_slot: str           # Q/W/E/R
    observed_at_game_time: float
    source: str                 # user_voice | 2999_event | 2999_state_diff
    confidence: str = "verified"


def ability_cast_support() -> CastSupport:
    return ABILITY_CAST_SUPPORT


def should_auto_track_summoner_spells() -> bool:
    """召唤师技能自动追踪开关。默认 False（政策风险）。"""
    return SUMMONER_SPELL_AUTO_TRACK_ENABLED
