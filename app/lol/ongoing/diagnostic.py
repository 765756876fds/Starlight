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
from typing import Any, Callable, Deque, Dict, List, Optional


class ChampSelectDiagnostic:
    def __init__(
        self,
        lcu_request_raw: Callable[[str, str, Any], Any],
        capture_dir: str = "runtime",
        interval: float = 0.5,
        ring_seconds: float = 120.0,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self._req = lcu_request_raw  # (method, endpoint, data) -> (status, body)
        self._dir = capture_dir
        self._interval = interval
        self._ring: Deque[Dict[str, Any]] = deque(maxlen=int(ring_seconds / interval))
        self._on_event = on_event or (lambda c, d: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_session_exists = False
        self._capture_seq = 0
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

    def ring(self) -> List[Dict[str, Any]]:
        return list(self._ring)

    def _run(self):
        last_phase = None
        while not self._stop.wait(self._interval):
            try:
                self._tick()
            except Exception as e:
                self._on_event("cs_diag.error", {"err": str(e)})

    def _tick(self):
        # gameflow phase
        t0 = time.time()
        gs, gb = self._req("GET", "/lol-gameflow/v1/gameflow-phase", None)
        phase = gb if isinstance(gb, str) and not isinstance(gb, (dict, list)) else (gb or "")
        phase = str(phase).strip('"')
        elapsed_g = int((time.time() - t0) * 1000)

        # champ-select session
        t1 = time.time()
        ss, sb = self._req("GET", "/lol-champ-select/v1/session", None)
        elapsed_s = int((time.time() - t1) * 1000)
        players: List[Dict] = []
        puuid_count = 0
        if ss == 200 and isinstance(sb, dict):
            for team in ("myTeam", "theirTeam"):
                for cell in sb.get(team) or []:
                    pu = cell.get("puuid") or ""
                    players.append({
                        "puuid": pu,
                        "name": cell.get("name") or "",
                        "team": team,
                        "championId": cell.get("championId"),
                        "assignedPosition": cell.get("assignedPosition"),
                    })
                    if pu:
                        puuid_count += 1
            unique = len({p["puuid"] for p in players if p["puuid"]})
        else:
            unique = 0

        now = time.time()
        entry = {
            "ts": now,
            "gameflow_status": gs,
            "gameflow_phase": phase,
            "cs_status": ss,
            "cs_elapsed_ms": elapsed_s,
            "player_count": len(players),
            "unique_puuid": unique,
        }
        self._ring.append(entry)

        # 状态变化
        if phase != entry.get("_last_phase"):
            self._on_event("cs_diag.phase", {"phase": phase, "gameflow_status": gs})
            entry["_last_phase"] = phase

        # session 200 就 capture（不等 10 人）
        if ss == 200:
            self._save_capture(phase, ss, players, unique, now)
            if unique >= 10 and not self._last_session_exists:
                self._on_event("cs_diag.room_complete", {
                    "players": len(players), "unique": unique,
                })
        self._last_session_exists = (ss == 200)

    def _save_capture(self, phase, status, players, unique, now):
        self._capture_seq += 1
        doc = {
            "capture_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "seq": self._capture_seq,
            "gameflow_phase": phase,
            "cs_http_status": status,
            "session_exists": True,
            "player_count": len(players),
            "unique_puuid_count": unique,
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
