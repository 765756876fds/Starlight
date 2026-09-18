"""自动化数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AutomationState(Enum):
    IDLE = "idle"
    CLIENT_DISCONNECTED = "client_disconnected"
    CLIENT_CONNECTED = "client_connected"
    IN_LOBBY = "in_lobby"
    QUEUE_SEARCHING = "queue_searching"
    MATCH_FOUND = "match_found"
    READY_CHECK = "ready_check"
    ACCEPTED = "accepted"
    CHAMP_SELECT = "champ_select"
    BAN_PHASE = "ban_phase"
    PICK_PHASE = "pick_phase"
    LOCKED = "locked"
    IN_GAME = "in_game"
    SETTLEMENT = "settlement"
    RETURN_TO_CLIENT = "return_to_client"
    SAFE_STOP = "safe_stop"


class ActionOutcome(Enum):
    ACTION_CONFIRMED = "action_confirmed"
    ACTION_CHANGED = "action_changed"
    ACTION_REJECTED = "action_rejected"
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(slots=True)
class PendingAction:
    action_id: int
    action_type: str  # "ban" | "pick"
    champion_id: int
    actor_cell_id: int
    attempts: int = 0
    # 'none' 尚未发 PATCH / 'sent' 上次 PATCH 2xx 等待回写 / 'failed' 上次非 2xx
    last_patch_status: str = "none"


@dataclass(slots=True)
class AutomationPreset:
    ban_priority: List[int] = field(default_factory=list)
    pick_priority: List[int] = field(default_factory=list)
    primary_role: Optional[str] = None
    secondary_role: Optional[str] = None


@dataclass(slots=True)
class AutomationConfig:
    enabled: bool = False
    auto_queue: bool = False
    auto_accept: bool = False
    auto_ban: bool = False
    auto_pick: bool = False
    accept_delay: float = 0.0
    max_action_attempts: int = 3
    polling_interval: float = 0.5
    presets: Dict[str, AutomationPreset] = field(default_factory=dict)
    auto_queue_mode: str = "bot"
    auto_queue_primary_role: Optional[str] = None
    auto_queue_secondary_role: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "AutomationConfig":
        data = data or {}
        presets_raw = data.get("presets") or {}
        presets: Dict[str, AutomationPreset] = {}
        for name, pv in presets_raw.items():
            pv = pv or {}
            presets[name] = AutomationPreset(
                ban_priority=list(pv.get("ban_priority") or []),
                pick_priority=list(pv.get("pick_priority") or []),
                primary_role=pv.get("primary_role"),
                secondary_role=pv.get("secondary_role"),
            )
        return cls(
            enabled=bool(data.get("enabled", False)),
            auto_queue=bool(data.get("auto_queue", False)),
            auto_accept=bool(data.get("auto_accept", False)),
            auto_ban=bool(data.get("auto_ban", False)),
            auto_pick=bool(data.get("auto_pick", False)),
            accept_delay=float(data.get("accept_delay", 0.0)),
            max_action_attempts=int(data.get("max_action_attempts", 3)),
            polling_interval=float(data.get("polling_interval", 0.5)),
            presets=presets,
            auto_queue_mode=str(data.get("auto_queue_mode", "bot")),
            auto_queue_primary_role=data.get("auto_queue_primary_role"),
            auto_queue_secondary_role=data.get("auto_queue_secondary_role"),
        )

    def pick_priority_for(self, role: Optional[str]) -> List[int]:
        # 取最近的 preset：优先 primary_role，再 secondary_role，最后任意一个非空
        candidates = [role] if role else []
        candidates += [p.primary_role for p in self.presets.values() if p.primary_role]
        seen = set()
        for r in candidates:
            if not r or r in seen:
                continue
            seen.add(r)
            if r in self.presets and self.presets[r].pick_priority:
                return list(self.presets[r].pick_priority)
        # fallback: 第一个非空 pick_priority
        for p in self.presets.values():
            if p.pick_priority:
                return list(p.pick_priority)
        return []

    def ban_priority_for(self, role: Optional[str]) -> List[int]:
        candidates = [role] if role else []
        candidates += [p.primary_role for p in self.presets.values() if p.primary_role]
        seen = set()
        for r in candidates:
            if not r or r in seen:
                continue
            seen.add(r)
            if r in self.presets and self.presets[r].ban_priority:
                return list(self.presets[r].ban_priority)
        for p in self.presets.values():
            if p.ban_priority:
                return list(p.ban_priority)
        return []
