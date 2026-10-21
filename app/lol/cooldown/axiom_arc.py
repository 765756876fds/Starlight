"""AxiomArcEvaluator — ChampionKill → Axiom Arc 退款评估器。

职责：
  ChampionKill Event
    ↓ 判断是否存在 Axiom Arc 持有者
    ↓ 判断该持有者是否有 ACTIVE R reminder
    ↓ 判断该 R cast snapshot 中是否拥有 Axiom Arc
    ↓ 计算 Lethality
    ↓ 计算退款
    ↓ 发布 cooldown.adjustment

注意：
- trigger_mode = "approximate_trigger"（2999 无法验证 3 秒伤害条件）
- 只认 SR 模式 itemID = 6696（不认 226696 Arena 变体）
- 身份匹配使用 summoner_name / riot_game_name（dual_name_match）
- 英雄匹配使用 ChampionCatalog 统一解析（英雄名 ↔ 称号）
"""
from __future__ import annotations

from collections.abc import Callable

from ...core.event_bus import Event, EventType
from ..data.champion_catalog import ChampionCatalog
from ..data.item_stats import sum_lethality
from .calculator import calculate_axiom_arc_refund
from .manager import CooldownManager

# Axiom Arc SR itemID
AXIOM_ARC_ITEM_ID = 6696

# trigger_mode 标记
TRIGGER_MODE = "approximate_trigger"


class AxiomArcEvaluator:
    """ChampionKill → Axiom Arc 退款评估器。
    
    使用方式：
        evaluator = AxiomArcEvaluator(
            cooldown_manager=manager,
            event_bus=bus,
            get_snapshot=lambda: runtime.current_snapshot,
        )
        evaluator.start()  # 订阅事件
    """
    
    def __init__(
        self,
        cooldown_manager: CooldownManager,
        event_bus,
        get_snapshot: Callable[[], object | None],
        champion_catalog: ChampionCatalog | None = None,
    ):
        self._cm = cooldown_manager
        self._bus = event_bus
        self._get_snapshot = get_snapshot
        self._champions = champion_catalog
        self._running = False
    
    def start(self):
        """订阅 ChampionKill 事件。"""
        if self._running:
            return
        self._bus.subscribe(EventType.CHAMPION_KILL, self._on_champion_kill)
        self._running = True
        print("[AxiomArc] evaluator started")
    
    def stop(self):
        """取消订阅。"""
        if not self._running:
            return
        self._bus.unsubscribe(EventType.CHAMPION_KILL, self._on_champion_kill)
        self._running = False
        print("[AxiomArc] evaluator stopped")
    
    def _on_champion_kill(self, event: Event):
        """处理 ChampionKill 事件。"""
        try:
            self._evaluate(event)
        except Exception as e:
            print(f"[AxiomArc] evaluate error: {e}")
    
    def _evaluate(self, event: Event):
        """核心评估逻辑。"""
        # 1. 提取事件数据
        event_id = f"axiom_{event.get('EventID', 'unknown')}"
        killer_name = event.get("KillerName", "")
        assisters = event.get("Assisters", []) or []
        
        if not killer_name:
            return
        
        # 2. 获取当前 snapshot
        snapshot = self._get_snapshot()
        if snapshot is None:
            return
        
        # 3. 获取所有 ACTIVE R reminders
        active_reminders = self._cm.list_active()
        # R 的显示名是 "大招"（中文）或 "R"（英文）
        r_reminders = [r for r in active_reminders 
                       if r.skill in ("大招", "R")]
        
        if not r_reminders:
            return
        
        # 4. 收集所有参与者（Killer + Assisters）
        participants = set()
        if killer_name:
            participants.add(killer_name)
        for a in assisters:
            if a:
                participants.add(a)
        
        # 5. 对每个 R reminder，检查持有者是否在参与者中
        for rem in r_reminders:
            # 5.1 检查 cast_snapshot
            if rem.cast_snapshot is None:
                continue
            
            # 5.2 检查 cast 时是否拥有 Axiom Arc
            items = rem.cast_snapshot.items
            if AXIOM_ARC_ITEM_ID not in items:
                continue
            
            # 5.3 获取该 reminder 对应的玩家身份
            # 通过 champion_name 匹配到 player
            player = self._find_player_by_champion(snapshot, rem.champion_name)
            if player is None:
                continue
            
            # 5.4 检查该玩家是否在参与者中
            # 注意：eventdata 的参与者名字格式不一致：
            # - 真人：riotGameName（不带 tagline），如 "空白书上书空白"
            # - BOT：summonerName，如 "潮汐海灵（电脑）"
            # 因此需要同时检查两个字段
            player_identities = {
                player.summoner_name,
                player.riot_game_name,
            }
            if not player_identities.intersection(participants):
                continue
            
            # 5.5 计算总 Lethality（从 cast_snapshot.items）
            total_lethality = sum_lethality(items)
            
            # 5.6 计算退款
            refund = calculate_axiom_arc_refund(
                total_ultimate_cooldown=rem.estimated_cooldown,
                total_lethality=total_lethality,
            )
            
            if refund <= 0:
                continue
            
            # 5.7 发布 cooldown.adjustment 事件
            self._bus.publish(Event(
                event_type=EventType.COOLDOWN_ADJUSTMENT,
                data={
                    "reminder_id": rem.id,
                    "adjustment_seconds": refund,
                    "event_id": event_id,
                    "source": "axiom_arc",
                    "trigger_mode": TRIGGER_MODE,
                    "champion": rem.champion_name,
                    "spell": "R",
                    "lethality": total_lethality,
                }
            ))
            
            matched_name = player_identities.intersection(participants).pop()
            
            print(f"[AxiomArc] ChampionKill EventID={event.get('EventID')}")
            print(f"[AxiomArc]   holder={matched_name} ({rem.champion_name})")
            print(f"[AxiomArc]   lethality={total_lethality}, refund={refund:.1f}s")
            print(f"[AxiomArc]   trigger_mode={TRIGGER_MODE}, reminder_id={rem.id}")
    
    def _find_player_by_champion(self, snapshot, champion_name: str):
        """通过 champion_name 查找 player（使用 ChampionCatalog 统一解析）。"""
        # 1. 先尝试精确匹配
        for p in snapshot.players:
            if p.champion_name == champion_name:
                return p
        
        # 2. 如果有 ChampionCatalog，用 catalog 统一解析后匹配
        if self._champions is not None:
            rem_champ = self._champions.get_by_name(champion_name)
            if rem_champ is not None:
                for p in snapshot.players:
                    p_champ = self._champions.get_by_name(p.champion_name)
                    if p_champ is not None and p_champ.cid == rem_champ.cid:
                        return p
        
        return None
