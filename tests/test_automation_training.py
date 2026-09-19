"""Automation 状态机回归：训练模式完全忽略；正常排位走队列。"""
import sys
sys.path.insert(0, ".")
from unittest.mock import MagicMock
from app.lol.automation.service import LoLAutomationService
from app.lol.automation.models import AutomationConfig


class FakeLCU:
    connected = True
    def __init__(self):
        self.phase = "Lobby"
    def get_gameflow_phase(self):
        return self.phase


def make_service(phase="Lobby", lobby_gamemode="MATCHED", is_custom=False,
                 auto_queue=True, auto_pick=True, auto_ban=True, auto_accept=True):
    lcu = FakeLCU()
    lcu.phase = phase
    cfg = AutomationConfig()
    cfg.enabled = True
    cfg.auto_queue = auto_queue
    cfg.auto_pick = auto_pick
    cfg.auto_ban = auto_ban
    cfg.auto_accept = auto_accept
    svc = LoLAutomationService.__new__(LoLAutomationService)
    # minimal init pieces
    svc._lcu = lcu
    svc._config = cfg
    svc._stop_event = __import__("threading").Event()
    svc._stop_event.set()  # don't run worker
    import threading
    svc._lock = threading.RLock()
    from app.core.event_bus import EventBus
    svc._bus = EventBus()
    from app.lol.automation.queue_controller import QueueController
    from app.lol.automation.ready_check_controller import ReadyCheckController
    from app.lol.automation.champ_select_controller import ChampSelectController
    from app.lol.automation.action_executor import ChampSelectActionExecutor
    svc._request_calls = []
    def req(method, endpoint, data):
        svc._request_calls.append((method, endpoint))
        if endpoint == "/lol-lobby/v2/lobby":
            return 200, {"gameConfig": {"gameMode": lobby_gamemode, "isCustom": is_custom}}
        if endpoint == "/lol-lobby/v2/lobby/matchmaking/search-state":
            return 200, {"isCurrentlyInQueue": False}
        if endpoint.endswith("/search"):
            return 200, {}
        return 200, {}
    svc._request_with_status = req
    svc._executor = MagicMock()
    svc._ready_check = MagicMock()
    svc._queue = QueueController(req, lambda *a: None)
    svc._champ_select = MagicMock()
    svc._state = "idle"
    svc._last_phase = "None"
    return svc


# --- Training mode: must be ignored in every phase ---
for ph in ("Lobby", "ChampSelect", "InProgress"):
    svc = make_service(phase=ph, lobby_gamemode="PRACTICETOOL")
    before = len(svc._request_calls)
    svc._tick()
    # 不应 POST search，不应 poll champ select
    posts = [c for c in svc._request_calls[before:] if c[1].endswith("/search") and c[0] == "POST"]
    assert posts == [], (ph, posts)
    assert not svc._champ_select.poll.called, ph
    print(f"T-T training {ph}: ignored OK")

# T-T7: exit ChampSelect -> back to training lobby -> next tick still no action
svc = make_service(phase="ChampSelect", lobby_gamemode="PRACTICETOOL")
svc._tick(); svc._champ_select.reset_mock()
svc._lcu.phase = "Lobby"
svc._tick()
assert not svc._champ_select.poll.called
assert not any(c[1].endswith("/search") and c[0] == "POST" for c in svc._request_calls)
print("T-T7 exit->lobby still ignored OK")

# --- Normal flow ---
# T-A2/A3: Lobby + FLEX => POST search
svc = make_service(phase="Lobby", lobby_gamemode="MATCHED")
svc._tick()
assert any(c[1].endswith("/search") and c[0] == "POST" for c in svc._request_calls), svc._request_calls
print("T-A3 normal lobby -> auto queue OK")

# T-A1: auto_queue off -> no POST
svc = make_service(phase="Lobby", lobby_gamemode="MATCHED", auto_queue=False)
n0 = len(svc._request_calls)
svc._tick()
assert not any(c[1].endswith("/search") and c[0] == "POST" for c in svc._request_calls[n0:])
print("T-A1 auto_queue off -> no queue OK")

# T-A6: normal ChampSelect -> poll called
svc = make_service(phase="ChampSelect", lobby_gamemode="MATCHED")
svc._tick()
assert svc._champ_select.poll.called
print("T-A6 normal champselect -> takeover OK")

# T-A5: ReadyCheck -> accept called
svc = make_service(phase="ReadyCheck", lobby_gamemode="MATCHED")
svc._tick()
assert svc._ready_check.check.called
print("T-A5 readycheck -> accept OK")

print("ALL automation regression OK")
