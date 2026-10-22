"""Champ Select 主循环：轮询 → 找我的动作 → 选候选 → 两阶段 PATCH → 校验。"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .action_executor import ChampSelectActionExecutor
from .models import AutomationConfig, PendingAction
from .priority import pick_available

SESSION_URL = "/lol-champ-select/v1/session"
PICKABLE_URL = "/lol-champ-select/v1/pickable-champion-ids"
BANNABLE_URL = "/lol-champ-select/v1/bannable-champion-ids"
CURRENT_SUMMONER_URL = "/lol-summoner/v1/current-summoner"


class ChampSelectController:
    def __init__(
        self,
        request_with_status: Callable[[str, str, dict | None], tuple],
        executor: ChampSelectActionExecutor,
        stop_event: threading.Event,
        on_event: Callable[[str, dict], None],
    ):
        self._request = request_with_status
        self._executor = executor
        self._stop = stop_event
        self._on_event = on_event

        self._pending: dict[int, PendingAction] = {}
        self._rejected: dict[int, set[int]] = {}
        self._no_candidate_actions: set[int] = set()
        # 按 action_id 缓存 (pickable, bannable)：同一动作只在首次需要候选时拉一次，
        # 未轮到我/无候选的 tick 不再连发 pickable/bannable GET
        self._server_sets: dict[int, tuple] = {}

    # ---- 状态清理 ----
    def clear_state(self) -> None:
        self._pending.clear()
        self._rejected.clear()
        self._no_candidate_actions.clear()
        self._server_sets.clear()

    def _clear_action(self, action_id: int) -> None:
        self._pending.pop(action_id, None)
        self._rejected.pop(action_id, None)
        self._no_candidate_actions.discard(action_id)
        self._server_sets.pop(action_id, None)

    # ---- 主 tick ----
    def poll(self, config: AutomationConfig) -> None:
        status, session = self._request("GET", SESSION_URL, None)

        if status == 404:
            # champ-select 消失 → 清理
            if self._pending or self._rejected:
                self.clear_state()
            return
        if status != 200 or not isinstance(session, dict):
            # transient failure：保留 pending
            self._on_event("automation.transient_failure", {"what": "session_get", "status": status})
            return

        local_cell = self._local_cell_id(session)
        if local_cell is None:
            return

        completed_bans = self._completed_bans(session)
        current_picks = self._current_picks(session)

        # pickable/bannable 不再无条件每 tick 拉取：延后到"我的进行中动作需要选候选"时按需拉取

        all_actions = [a for group in session.get("actions", []) for a in group]
        live_action_ids = {int(a.get("id", -1)) for a in all_actions}

        # 清理已经不在 session 中的 pending
        for aid in list(self._pending.keys()):
            if aid not in live_action_ids:
                self._clear_action(aid)

        # 先检查已 pending 的动作是否已完成
        for aid, pending in list(self._pending.items()):
            action = next((a for a in all_actions if int(a.get("id", -1)) == aid), None)
            if action is not None and action.get("completed"):
                self._verify_completed(action, pending)
                continue

        # 前置短路：没有"我的进行中动作"（未轮到我 / 队友操作中）→ 本轮不再请求 pickable/bannable
        if not any(
            int(a.get("actorCellId", -1)) == local_cell
            and a.get("isInProgress", False)
            and not a.get("completed")
            for a in all_actions
        ):
            return

        # 找当前我的进行中动作
        for action in all_actions:
            aid = int(action.get("id", -1))
            pending = self._pending.get(aid)

            # 已完成 → 校验结果
            if action.get("completed"):
                if pending is not None:
                    self._verify_completed(action, pending)
                continue

            # 不是我的进行中动作，跳过
            if action.get("actorCellId") != local_cell:
                continue
            if not action.get("isInProgress", False):
                continue

            # 已有 pending：用同一 champion 重试（bounded retry）
            if pending is not None:
                self._attempt_action(
                    aid, pending.champion_id, pending.action_type,
                    pending.actor_cell_id, False, config,
                )
                return

            atype = action.get("type")
            if atype == "ban" and config.auto_ban:
                priority = config.ban_priority_for(None)
                unavailable: set[int] = set(completed_bans)
                server_set = self._server_set_for(aid, BANNABLE_URL, 1)
                event_code = "automation.ban_attempted"
            elif atype == "pick" and config.auto_pick:
                priority = config.pick_priority_for(None)
                unavailable = set(completed_bans) | set(current_picks)
                server_set = self._server_set_for(aid, PICKABLE_URL, 0)
                event_code = "automation.pick_attempted"
            else:
                continue

            rejected = self._rejected.get(aid, set())
            candidate = pick_available(priority, rejected | unavailable, server_set)
            if candidate is None:
                if aid not in self._no_candidate_actions:
                    self._no_candidate_actions.add(aid)
                    self._on_event(
                        "automation.no_available_champion",
                        {"action_id": aid, "type": atype},
                    )
                return
            self._no_candidate_actions.discard(aid)

            self._on_event(
                event_code,
                {"action_id": aid, "champion_id": candidate, "type": atype},
            )
            self._attempt_action(
                aid, candidate, atype, local_cell, server_set is not None, config
            )
            return

    # ---- 执行 ----
    def _attempt_action(
        self,
        action_id: int,
        champion_id: int,
        action_type: str,
        actor_cell_id: int,
        server_known: bool,
        config: AutomationConfig,
    ) -> None:
        pending = self._pending.get(action_id)
        if pending is None or pending.champion_id != champion_id:
            pending = PendingAction(action_id, action_type, champion_id, actor_cell_id)
            self._pending[action_id] = pending

        # 上次 PATCH 已 2xx，等待 LCU 回写 completed：不再重发，不消耗 attempts
        if pending.last_patch_status == "sent":
            return

        if pending.attempts >= config.max_action_attempts:
            self._reject(pending)
            return

        pending.attempts += 1

        ok = self._executor.two_phase_patch(
            action_id, champion_id,
            hover_only=False,
        )
        if ok:
            pending.last_patch_status = "sent"
        else:
            # 阶段 1 失败，或 stop 在阶段 1.5 触发
            if self._stop.is_set():
                return
            pending.last_patch_status = "failed"
            if pending.attempts >= config.max_action_attempts:
                self._reject(pending)
            # 否则保留 pending，下一轮 polling 再试

    def _reject(self, pending: PendingAction) -> None:
        self._pending.pop(pending.action_id, None)
        self._rejected.setdefault(pending.action_id, set()).add(pending.champion_id)
        self._on_event(
            "automation.action_rejected",
            {
                "action_id": pending.action_id,
                "champion_id": pending.champion_id,
                "type": pending.action_type,
            },
        )

    # ---- 完成校验 ----
    def _verify_completed(self, action: dict, pending: PendingAction) -> None:
        actual_id = int(action.get("championId") or 0)
        matches = (
            action.get("type") == pending.action_type
            and int(action.get("actorCellId", -1)) == pending.actor_cell_id
            and actual_id == pending.champion_id
            and bool(action.get("completed"))
        )
        if matches:
            self._clear_action(pending.action_id)
            self._on_event(
                "automation.action_confirmed",
                {
                    "action_id": pending.action_id,
                    "champion_id": pending.champion_id,
                    "type": pending.action_type,
                },
            )
        else:
            # 先加 rejected（保留已有的 rejected 集合），再单独清 pending。
            # 绝不能调 _clear_action —— 它会连 rejected 一起清掉。
            self._rejected.setdefault(pending.action_id, set()).add(pending.champion_id)
            self._pending.pop(pending.action_id, None)
            self._no_candidate_actions.discard(pending.action_id)
            self._on_event(
                "automation.action_changed",
                {
                    "expected": pending.champion_id,
                    "actual": actual_id,
                    "type": pending.action_type,
                },
            )

    # ---- helpers ----
    def _local_cell_id(self, session: dict) -> int | None:
        v = session.get("localPlayerCellId")
        if v is not None:
            return int(v)
        status, me = self._request("GET", CURRENT_SUMMONER_URL, None)
        if status != 200 or not isinstance(me, dict):
            return None
        sid = me.get("summonerId")
        for m in session.get("myTeam", []):
            if m.get("summonerId") == sid:
                return int(m.get("cellId"))
        return None

    def _fetch_set(self, url: str) -> set[int] | None:
        # 与 Syronss bot.py 的 _get_server_available 一致：拉取失败不阻断动作，
        # 由 PATCH 结果决定 reject。不做连续失败熔断。
        status, body = self._request("GET", url, None)
        if status != 200 or not isinstance(body, list):
            return None
        try:
            return {int(x) for x in body}
        except (TypeError, ValueError):
            return None

    def _server_set_for(self, action_id: int, url: str, slot: int) -> set[int] | None:
        """按 action_id 缓存 (pickable, bannable)：

        - 同一动作只在首次需要选候选时拉一次服务器集合；
        - 后续 tick 未轮到我/无候选时直接复用缓存，不再发 GET；
        - 动作完成（_clear_action）或离开选人（clear_state）后缓存失效。
        slot=0 为 pickable、slot=1 为 bannable。
        """
        cached = self._server_sets.get(action_id)
        if cached is not None and cached[slot] is not None:
            return cached[slot]
        val = self._fetch_set(url)
        if cached is None:
            cached = (None, None)
        cached = list(cached)
        cached[slot] = val
        self._server_sets[action_id] = tuple(cached)
        return val

    @staticmethod
    def _completed_bans(session: dict) -> set[int]:
        out: set[int] = set()
        for group in session.get("actions", []):
            for a in group:
                if a.get("type") == "ban" and a.get("completed"):
                    cid = int(a.get("championId") or 0)
                    if cid:
                        out.add(cid)
        return out

    @staticmethod
    def _current_picks(session: dict) -> set[int]:
        out: set[int] = set()
        for side in ("myTeam", "theirTeam"):
            for m in session.get(side, []):
                cid = int(m.get("championId") or 0)
                if cid:
                    out.add(cid)
                intent = int(m.get("championPickIntent") or 0)
                if intent:
                    out.add(intent)
        return out
