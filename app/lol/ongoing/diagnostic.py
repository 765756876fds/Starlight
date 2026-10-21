"""Champion Select 自动诊断观察器（只读，不改业务触发）。

目的：
- 同时轮询 gameflow-phase 与 champ-select/session，记录状态变化；
- session==200 即立刻落盘 runtime capture（不等 10 人）；
- session 达到 unique_puuid>=10 时记 ROOM_COMPLETE；
- ring buffer 保留最近 ~120s 状态，供事后对账。

不保存 token / Authorization。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from app.core.path_resolve import PROJECT_ROOT


class ChampSelectDiagnostic:
    def __init__(
        self,
        lcu_request_raw: Callable[[str, str, Any], Any],
        capture_dir: str = str(PROJECT_ROOT / "runtime"),
        interval: float = 0.5,
        ring_seconds: float = 120.0,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self._req = lcu_request_raw  # (method, endpoint, data) -> (status, body)
        self._dir = capture_dir
        self._interval = interval
        self._ring: deque[dict[str, Any]] = deque(maxlen=int(ring_seconds / interval))
        self._on_event = on_event or (lambda c, d: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_session_exists = False
        self._capture_seq = 0
        self._timeline: deque[dict[str, Any]] = deque(maxlen=600)
        self._first_session_200: float | None = None
        os.makedirs(self._dir, exist_ok=True)

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cs-diagnostic")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    def ring(self) -> list[dict[str, Any]]:
        return list(self._ring)

    def _run(self):
        last_phase = None
        while not self._stop.wait(self._interval):
            try:
                self._tick()
            except Exception as e:
                self._on_event("cs_diag.error", {"err": str(e)})

    def _tick(self):
        # 1. gameflow phase
        gs, gb = self._req("GET", "/lol-gameflow/v1/gameflow-phase", None)
        phase = str(gb).strip('"') if gb is not None else ""

        # 2. gameflow session（含 gameId/queue/gameMode）
        gss, gsb = self._req("GET", "/lol-gameflow/v1/session", None)
        game_id = queue_id = game_mode = None
        if gss == 200 and isinstance(gsb, dict):
            gd = gsb.get("gameData") or {}
            q = gd.get("queue") or {}
            game_id = gd.get("gameId")
            queue_id = q.get("id")
            game_mode = gd.get("gameMode")

        # 3. champ-select session
        ss, sb = self._req("GET", "/lol-champ-select/v1/session", None)
        players: list[dict] = []
        if ss == 200 and isinstance(sb, dict):
            for team in ("myTeam", "theirTeam"):
                for cell in sb.get(team) or []:
                    players.append({
                        "puuid": cell.get("puuid") or "",
                        "name": cell.get("name") or "",
                        "team": team,
                        "championId": cell.get("championId"),
                        "assignedPosition": cell.get("assignedPosition"),
                    })
        unique = len({p["puuid"] for p in players if p["puuid"]})

        now = time.time()
        entry = {
            "ts": now,
            "gameflow_phase": phase,
            "gfs_status": gss,
            "game_id": game_id,
            "queue_id": queue_id,
            "game_mode": game_mode,
            "cs_status": ss,
            "player_count": len(players),
            "unique_puuid": unique,
            "case": self._classify(phase, ss, gss),
        }
        self._ring.append(entry)

        # 状态转换
        prev = self._ring[-2] if len(self._ring) > 1 else None
        if prev and prev["gameflow_phase"] != phase:
            self._on_event("cs_diag.transition", {
                "from": prev["gameflow_phase"], "to": phase,
                "game_id": game_id, "queue_id": queue_id,
            })

        # session 200 时间记录
        if ss == 200:
            if self._first_session_200 is None:
                self._first_session_200 = now
                self._on_event("cs_diag.first_session_200", {
                    "gameflow_phase": phase, "game_id": game_id,
                })
            self._save_capture(entry, players, now)
            if unique >= 10 and not self._last_session_exists:
                self._on_event("cs_diag.room_complete", {
                    "players": len(players), "unique": unique,
                })
        self._last_session_exists = (ss == 200)

    @staticmethod
    def _classify(phase, cs_status, gfs_status):
        if phase == "ChampSelect" and cs_status == 200:
            return "CASE_A_NORMAL"
        if phase == "InProgress" and cs_status == 200:
            return "CASE_B_CONFLICT"
        if phase == "InProgress" and cs_status == 404 and gfs_status == 200:
            return "CASE_C_NO_CS_API"
        if phase == "Lobby" and cs_status == 200:
            return "CASE_D_CONFLICT"
        if phase == "ChampSelect" and cs_status == 404:
            return "CASE_E_CONFLICT"
        if phase == "GameStart" and cs_status == 404:
            return "CASE_F_TRANSITION"
        return "CASE_OTHER"

    def _save_capture(self, entry, players, now):
        self._capture_seq += 1
        doc = {
            "capture_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "seq": self._capture_seq,
            "gameflow_phase": entry["gameflow_phase"],
            "gfs_status": entry["gfs_status"],
            "game_id": entry["game_id"],
            "queue_id": entry["queue_id"],
            "game_mode": entry["game_mode"],
            "cs_http_status": entry["cs_status"],
            "session_exists": True,
            "player_count": len(players),
            "unique_puuid_count": entry["unique_puuid"],
            "case": entry["case"],
            "players": players,
        }
        fn = os.path.join(
            self._dir,
            f"ongoing_capture_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{self._capture_seq:04d}.json",
        )
        try:
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
