"""OngoingGameService：独立轮询 gameflow-phase，不依赖 automation 事件。"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable, List, Optional

from .loader import OngoingGameLoader
from .diagnostic import ChampSelectDiagnostic
from .models import PlayerSnapshot


class OngoingGameService:
    def __init__(
        self,
        lcu=None,
        on_event: Optional[Callable[[str, dict], None]] = None,
        cache_ttl: float = 300.0,
        max_workers: int = 4,
        rps: float = 20.0,
        recent_n: int = 20, account_history_service=None,
        poll_interval: float = 1.0,
    ):
        self._lcu = lcu
        self._on_event = on_event or (lambda c, d: None)
        getter = (lcu.get if lcu is not None else (lambda ep: None))
        self._loader = OngoingGameLoader(
            lcu_get=getter,
            on_event=self._on_event,
            max_workers=max_workers,
            rps=rps,
            recent_matches_count=recent_n, account_history_service=account_history_service,
        )
        self._snapshots: List[PlayerSnapshot] = []
        self._current_session_key: Optional[str] = None
        self._loading_in_progress = False
        self._poll_interval = poll_interval

        self._diag = ChampSelectDiagnostic(lcu.request_raw, on_event=self._on_event) if lcu is not None else None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ----- lifecycle -----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        if self._diag: self._diag.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._diag: self._diag.stop()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ----- poll loop -----
    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self._tick()
            except Exception as e:
                self._on_event("ongoing.error", {"err": str(e)})

    def _tick(self) -> None:
        if self._lcu is None:
            return
        phase = self._lcu.get("/lol-gameflow/v1/gameflow-phase")
        if phase != "ChampSelect":
            with self._lock:
                if self._current_session_key is not None or self._snapshots:
                    self._current_session_key = None
                    self._snapshots = []
                    self._loading_in_progress = False
            return

        # phase == ChampSelect
        session = self._lcu.get("/lol-champ-select/v1/session")
        if not isinstance(session, dict):
            return
        key = self._session_key(session)
        with self._lock:
            if key == self._current_session_key:
                return  # 同一局，已加载/加载中
            if self._loading_in_progress:
                return
            self._current_session_key = key
            self._loading_in_progress = True
            self._snapshots = []
        t = threading.Thread(target=self._load_in_thread, args=(session,), daemon=True)
        t.start()

    @staticmethod
    def _session_key(session: dict) -> str:
        game_id = session.get("gameId")
        if game_id:
            return f"gid:{game_id}"
        # fallback: puuid 集合 hash
        puuids = sorted(
            (c.get("puuid") or "")
            for c in (session.get("myTeam") or []) + (session.get("theirTeam") or [])
            if c.get("puuid")
        )
        h = hashlib.md5(",".join(puuids).encode()).hexdigest()[:12]
        return f"hash:{h}"

    def _load_in_thread(self, session: dict) -> None:
        try:
            snaps = self._loader.load_for_champ_select(session)
            with self._lock:
                self._snapshots = snaps
        except Exception as e:
            self._on_event("ongoing.error", {"err": str(e)})
        finally:
            with self._lock:
                self._loading_in_progress = False

    # ----- UI -----
    def get_snapshots(self) -> List[PlayerSnapshot]:
        with self._lock:
            return list(self._snapshots)

    def status(self) -> dict:
        return self._loader.status()
