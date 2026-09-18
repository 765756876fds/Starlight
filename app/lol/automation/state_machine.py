"""AutomationState 状态推导：纯函数，从 gameflow phase 推导 AutomationState。"""
from __future__ import annotations

from .models import AutomationState


def derive_state(phase: str) -> AutomationState:
    """根据 LCU gameflow-phase 返回对应 AutomationState。"""
    if phase == "None":
        return AutomationState.IDLE
    if phase == "Lobby":
        return AutomationState.IN_LOBBY
    if phase in ("Matchmaking", "CheckedIntoTournament"):
        return AutomationState.QUEUE_SEARCHING
    if phase == "ReadyCheck":
        return AutomationState.READY_CHECK
    if phase == "ChampSelect":
        return AutomationState.CHAMP_SELECT
    if phase in ("InProgress", "Reconnecting"):
        return AutomationState.IN_GAME
    if phase in ("PreEndOfGame", "EndOfGame"):
        return AutomationState.SETTLEMENT
    if phase == "LobbyClient":
        return AutomationState.RETURN_TO_CLIENT
    return AutomationState.IDLE
