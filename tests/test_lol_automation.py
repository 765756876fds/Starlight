"""Tests for app/lol/automation.

Uses a fake LCU connector that records (method, endpoint, body) and returns
scripted (status, body) tuples. No real network / no real LCU.
"""
from __future__ import annotations

import threading

import pytest

from app.lol.automation.action_executor import ChampSelectActionExecutor
from app.lol.automation.champ_select_controller import ChampSelectController
from app.lol.automation.models import AutomationConfig, AutomationState
from app.lol.automation.queue_controller import QueueController
from app.lol.automation.ready_check_controller import ReadyCheckController
from app.lol.automation.priority import pick_available
from app.lol.automation.models import PendingAction
from app.lol.automation.service import LoLAutomationService


class FakeLcu:
    """Records request_raw calls; scripts responses by endpoint."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.routes: dict[tuple, tuple] = {}
        self.connected = True
        self.gameflow_phase = "None"

    def route(self, method: str, endpoint: str, status: int, body):
        self.routes[(method, endpoint)] = (status, body)

    def request_raw(self, method: str, endpoint: str, data=None):
        self.calls.append((method, endpoint, data))
        # exact match
        if (method, endpoint) in self.routes:
            return self.routes[(method, endpoint)]
        # prefix match for dynamic actions/{id}
        for (m, ep), resp in self.routes.items():
            if m == method and endpoint.startswith(ep):
                return resp
        return 0, None

    def get_gameflow_phase(self):
        return self.gameflow_phase


def make_events():
    out = []
    def on(code, payload):
        out.append((code, payload))
    return out, on


def active_pick(champion_id=0, completed=False, action_id=9):
    return {
        "localPlayerCellId": 3,
        "myTeam": [{"cellId": 3, "championId": champion_id}],
        "theirTeam": [],
        "actions": [
            [
                {
                    "id": action_id,
                    "type": "pick",
                    "actorCellId": 3,
                    "isInProgress": not completed,
                    "completed": completed,
                    "championId": champion_id,
                }
            ]
        ],
    }


# ============================================================
# Priority
# ============================================================

def test_pick_available_skips_rejected_and_server():
    assert pick_available([1, 2, 3], rejected={2}, server_available={1, 2, 3}) == 1
    assert pick_available([1, 2, 3], rejected={1}, server_available={1, 2, 3}) == 2
    assert pick_available([1, 9], rejected=set(), server_available={1}) == 1
    assert pick_available([1, 2], rejected=set(), server_available=None) == 1
    assert pick_available([1, 2], rejected={1, 2}, server_available={1, 2}) is None


# ============================================================
# ReadyCheck
# ============================================================

def _ready_payload(state="InProgress", response="None", ready_id=1):
    return {"state": state, "playerResponse": response, "readyCheckId": ready_id}


def test_ready_check_accepts():
    lcu = FakeLcu()
    # first GET InProgress, second GET (revalidate) InProgress, POST accept 204
    lcu.route("GET", "/lol-matchmaking/v1/ready-check", 200, _ready_payload())
    lcu.route("POST", "/lol-matchmaking/v1/ready-check/accept", 204, None)
    ev, on = make_events()
    stop = threading.Event()
    rc = ReadyCheckController(lcu.request_raw, stop, on)
    rc.check(0.0)
    assert any(m == "POST" and ep == "/lol-matchmaking/v1/ready-check/accept"
               for m, ep, _ in lcu.calls)
    assert any(c == "automation.accept_sent" for c, _ in ev)


def test_ready_check_stop_during_delay_sends_no_post():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-matchmaking/v1/ready-check", 200, _ready_payload())
    ev, on = make_events()
    stop = threading.Event()
    rc = ReadyCheckController(lcu.request_raw, stop, on)
    # trigger in a thread that stops after match_found
    def run():
        rc.check(5.0)
    t = threading.Thread(target=run)
    t.start()
    # wait until active (ready_check_detected) then stop
    deadline = threading.Event()
    for _ in range(50):
        if any(c == "automation.ready_check_detected" for c, _ in ev):
            break
        threading.Event().wait(0.02)
    stop.set()
    t.join(timeout=3)
    assert not any(m == "POST" for m, _, _ in lcu.calls)


def test_ready_check_revalidation_failure_no_post():
    lcu = FakeLcu()
    # first GET InProgress, second GET state=None
    lcu.route("GET", "/lol-matchmaking/v1/ready-check", 200, _ready_payload())
    ev, on = make_events()
    stop = threading.Event()
    rc = ReadyCheckController(lcu.request_raw, stop, on)
    # make second GET return expired: use side-effect counter
    calls = {"n": 0}
    orig = lcu.request_raw
    def patched(method, endpoint, data=None):
        if method == "GET" and endpoint == "/lol-matchmaking/v1/ready-check":
            calls["n"] += 1
            if calls["n"] == 1:
                return 200, _ready_payload()
            return 200, _ready_payload(state="None", response="None")
        return orig(method, endpoint, data)
    rc2 = ReadyCheckController(patched, stop, on)
    rc2.check(0.0)
    assert not any(m == "POST" for m, _, _ in lcu.calls)
    assert any(c == "automation.ready_check_expired" for c, _ in ev)


# ============================================================
# Two-phase PATCH
# ============================================================

def test_two_phase_patch_full():
    lcu = FakeLcu()
    lcu.route("PATCH", "/lol-champ-select/v1/session/actions/9", 200, {})
    stop = threading.Event()
    ev, on = make_events()
    ex = ChampSelectActionExecutor(lcu.request_raw, stop, on)
    assert ex.two_phase_patch(9, 1, hover_only=False) is True
    patches = [c for c in lcu.calls if c[0] == "PATCH"]
    assert len(patches) == 2
    assert patches[0][2] == {"championId": 1}
    assert patches[1][2] == {"championId": 1, "completed": True}


def test_two_phase_patch_stop_between():
    lcu = FakeLcu()
    lcu.route("PATCH", "/lol-champ-select/v1/session/actions/9", 200, {})
    stop = threading.Event()
    ev, on = make_events()
    ex = ChampSelectActionExecutor(lcu.request_raw, stop, on)
    # set stop immediately; wait(0.15) returns True
    stop.set()
    result = ex.two_phase_patch(9, 1, hover_only=False)
    # hover succeeded, stop returned True → no phase 2
    assert result is False
    patches = [c for c in lcu.calls if c[0] == "PATCH"]
    assert len(patches) == 1  # only hover


# ============================================================
# ChampSelectController
# ============================================================

def _make_controller(lcu, ev, stop=None):
    stop = stop or threading.Event()
    ex = ChampSelectActionExecutor(lcu.request_raw, stop, ev)
    return ChampSelectController(lcu.request_raw, ex, stop, ev)


def _cfg(**kw):
    from app.lol.automation.models import AutomationPreset
    base = dict(enabled=True, auto_ban=False, auto_pick=True, max_action_attempts=3)
    base.update(kw)
    c = AutomationConfig(**base)
    c.presets["solo_duo"] = AutomationPreset(pick_priority=[1, 2], ban_priority=[1])
    return c


def test_champ_select_picks_first_available():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    lcu.route("PATCH", "/lol-champ-select/v1/session/actions/9", 200, {})
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    ctrl.poll(_cfg())
    patch_ids = [c[2]["championId"] for c in lcu.calls if c[0] == "PATCH"]
    assert patch_ids[0] == 1


def test_champ_select_skips_not_own_action():
    lcu = FakeLcu()
    s = active_pick()
    # set actor to another cell
    s["actions"][0][0]["actorCellId"] = 5
    lcu.route("GET", "/lol-champ-select/v1/session", 200, s)
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    ctrl.poll(_cfg())
    assert not any(c[0] == "PATCH" for c in lcu.calls)


def test_champ_select_wrong_action_type_ignored():
    lcu = FakeLcu()
    s = active_pick()
    s["actions"][0][0]["type"] = "ban"  # config only enables pick
    lcu.route("GET", "/lol-champ-select/v1/session", 200, s)
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    ctrl.poll(_cfg())
    assert not any(c[0] == "PATCH" for c in lcu.calls)


def test_champ_select_404_clears_state():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 404, None)
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    # seed pending
    ctrl._pending[9] = PendingAction(9, "pick", 1, 3)
    ctrl.poll(_cfg())
    assert 9 not in ctrl._pending


def test_champ_select_candidate_advances_to_next():
    lcu = FakeLcu()
    # first two-phase PATCH fails (status 400), second succeeds
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    # PATCH #1 fails (champion 1 hover); all later PATCHes succeed
    patch_calls = {"n": 0, "ids": []}
    orig = lcu.request_raw
    def patched(method, endpoint, data=None):
        if method == "PATCH":
            patch_calls["n"] += 1
            patch_calls["ids"].append(data.get("championId"))
            if patch_calls["n"] == 1:
                return 400, None
            return 200, {}
        return orig(method, endpoint, data)
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(patched, stop, on)
    ctrl = ChampSelectController(patched, ex, stop, on)
    cfg = _cfg(max_action_attempts=1)
    # poll 1: try champion 1, fails → rejected
    ctrl.poll(cfg)
    # poll 2: champion 1 rejected, try champion 2
    ctrl.poll(cfg)
    assert 1 in patch_calls["ids"] and 2 in patch_calls["ids"]


def test_champ_select_patch_ok_but_final_champion_mismatch():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    lcu.route("PATCH", "/lol-champ-select/v1/session/actions/9", 200, {})
    # first poll: action in progress, champion 0
    # second poll: completed with champion_id=2 (not our 1)
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())  # will be overridden
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(lcu.request_raw, stop, on)
    ctrl = ChampSelectController(lcu.request_raw, ex, stop, on)
    ctrl.poll(_cfg())
    # now mark completed with champion 2
    lcu.route("GET", "/lol-champ-select/v1/session", 200,
              active_pick(champion_id=2, completed=True))
    ctrl.poll(_cfg())
    assert any(c == "automation.action_changed" for c, _ in ev)


def test_champ_select_transient_failure_keeps_pending():
    lcu = FakeLcu()
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(lcu.request_raw, stop, on)
    ctrl = ChampSelectController(lcu.request_raw, ex, stop, on)
    # seed pending
    ctrl._pending[9] = PendingAction(9, "pick", 1, 3, attempts=1)
    # GET session returns non-200 (transient)
    lcu.route("GET", "/lol-champ-select/v1/session", 0, None)
    ctrl.poll(_cfg())
    assert 9 in ctrl._pending


def test_champ_select_disabled_no_patch():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    cfg = _cfg(auto_pick=False)
    ctrl.poll(cfg)
    assert not any(c[0] == "PATCH" for c in lcu.calls)


def test_champ_select_bounded_retry_same_candidate():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    # all PATCHes fail
    orig = lcu.request_raw
    def always_fail(method, endpoint, data=None):
        if method == "PATCH":
            return 400, None
        return orig(method, endpoint, data)
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(always_fail, stop, on)
    ctrl = ChampSelectController(always_fail, ex, stop, on)
    cfg = _cfg(max_action_attempts=2)
    ctrl.poll(cfg)  # attempts=1
    ctrl.poll(cfg)  # attempts=2 → reject
    assert any(c == "automation.action_rejected" for c, _ in ev)
    # champion 1 should be in rejected
    assert 1 in ctrl._rejected.get(9, set())


# ============================================================
# QueueController
# ============================================================

def test_queue_already_searching_no_post():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-lobby/v2/lobby/matchmaking/search-state", 200,
              {"isCurrentlyInQueue": True})
    ev, on = make_events()
    q = QueueController(lcu.request_raw, on)
    assert q.try_start("Lobby") is False
    assert not any(m == "POST" for m, _, _ in lcu.calls)


def test_queue_wrong_phase_no_post():
    lcu = FakeLcu()
    ev, on = make_events()
    q = QueueController(lcu.request_raw, on)
    assert q.try_start("ChampSelect") is False
    assert not any(m == "POST" for m, _, _ in lcu.calls)


def test_queue_start_posts():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-lobby/v2/lobby/matchmaking/search-state", 200,
              {"isCurrentlyInQueue": False})
    lcu.route("POST", "/lol-lobby/v2/lobby/matchmaking/search", 204, None)
    ev, on = make_events()
    q = QueueController(lcu.request_raw, on)
    assert q.try_start("Lobby") is True
    assert any(m == "POST" and ep == "/lol-lobby/v2/lobby/matchmaking/search"
               for m, ep, _ in lcu.calls)


# ============================================================
# FIX-1: PATCH 2xx 后不重发 / 完成确认 / 非 2xx 重试
# ============================================================

def test_patch_2xx_no_resend():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    lcu.route("PATCH", "/lol-champ-select/v1/session/actions/9", 200, {})
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    ctrl.poll(_cfg())
    n1 = sum(1 for c in lcu.calls if c[0] == "PATCH")
    ctrl.poll(_cfg())
    n2 = sum(1 for c in lcu.calls if c[0] == "PATCH")
    assert n1 == n2 == 2
    assert ctrl._pending[9].attempts == 1
    assert ctrl._pending[9].last_patch_status == "sent"


def test_patch_2xx_then_completed_confirms():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    lcu.route("PATCH", "/lol-champ-select/v1/session/actions/9", 200, {})
    ev, on = make_events()
    ctrl = _make_controller(lcu, on)
    ctrl.poll(_cfg())
    lcu.route("GET", "/lol-champ-select/v1/session", 200,
              active_pick(champion_id=1, completed=True))
    ctrl.poll(_cfg())
    assert any(c == "automation.action_confirmed" for c, _ in ev)
    assert 9 not in ctrl._pending


def test_patch_non2xx_retries_to_reject():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    orig = lcu.request_raw
    def always_fail(method, endpoint, data=None):
        if method == "PATCH":
            return 400, None
        return orig(method, endpoint, data)
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(always_fail, stop, on)
    ctrl = ChampSelectController(always_fail, ex, stop, on)
    cfg = _cfg(max_action_attempts=2)
    ctrl.poll(cfg)
    ctrl.poll(cfg)
    assert any(c == "automation.action_rejected" for c, _ in ev)
    assert 9 not in ctrl._pending
    assert 1 in ctrl._rejected.get(9, set())


def test_cross_session_rejected_not_polluted():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-champ-select/v1/session", 200, active_pick())
    lcu.route("GET", "/lol-champ-select/v1/pickable-champion-ids", 200, [1, 2])
    lcu.route("GET", "/lol-champ-select/v1/bannable-champion-ids", 200, [1, 2])
    orig = lcu.request_raw
    def always_fail(method, endpoint, data=None):
        if method == "PATCH":
            return 400, None
        return orig(method, endpoint, data)
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(always_fail, stop, on)
    ctrl = ChampSelectController(always_fail, ex, stop, on)
    cfg = _cfg(max_action_attempts=1)
    ctrl.poll(cfg)
    assert 1 in ctrl._rejected.get(9, set())
    ctrl.clear_state()
    assert 9 not in ctrl._rejected
    patched = {"n": 0}
    def patched2(method, endpoint, data=None):
        if method == "PATCH":
            patched["n"] += 1
            return 200, {}
        return orig(method, endpoint, data)
    ex2 = ChampSelectActionExecutor(patched2, stop, on)
    ctrl2 = ChampSelectController(patched2, ex2, stop, on)
    ctrl2.poll(cfg)
    assert ctrl2._pending[9].champion_id == 1


def test_service_clears_state_on_leaving_champ_select():
    class FakeLcu:
        connected = True
        def __init__(self): self.phase = "ChampSelect"
        def get_gameflow_phase(self): return self.phase
    from app.lol.automation.service import LoLAutomationService
    lcu = FakeLcu()
    cfg = AutomationConfig(enabled=True, auto_pick=True)
    svc = LoLAutomationService(lcu=lcu, config=cfg)
    svc._champ_select._pending[9] = PendingAction(9, "pick", 1, 3)
    svc._champ_select._rejected[9] = {1}
    svc._last_phase = "ChampSelect"
    lcu.phase = "InProgress"
    svc._tick()
    assert 9 not in svc._champ_select._pending
    assert 9 not in svc._champ_select._rejected


# ============================================================
# CONSOLIDATE: feature 独立开关 / 互不干扰
# ============================================================

def test_features_all_disabled_no_requests():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-summoner/v1/current-summoner", 200, {"summonerLevel": 1})
    lcu.route("GET", "/lol-gameflow/v1/gameflow-phase", 200, "Lobby")
    lcu.route("GET", "/lol-lobby/v2/lobby/matchmaking/search-state", 200, {"isCurrentlyInQueue": False})
    lcu.route("POST", "/lol-lobby/v2/lobby/matchmaking/search", 204, {})
    ev, on = make_events()
    svc = LoLAutomationService(lcu=lcu, config=AutomationConfig(enabled=True))
    svc._tick()
    writes = [c for c in lcu.calls if c[0] in ("POST", "PATCH")]
    assert writes == []
    st = svc.feature_status()
    assert st["auto_queue"]["enabled"] is False
    assert st["auto_accept"]["enabled"] is False


def test_feature_banner_smoke():
    LoLAutomationService.print_feature_banner(AutomationConfig(
        enabled=True, auto_queue=True, auto_accept=True, auto_ban=True, auto_pick=True))


def test_state_machine_derive():
    from app.lol.automation.state_machine import derive_state
    assert derive_state("None") == AutomationState.IDLE
    assert derive_state("Lobby") == AutomationState.IN_LOBBY
    assert derive_state("Matchmaking") == AutomationState.QUEUE_SEARCHING
    assert derive_state("ReadyCheck") == AutomationState.READY_CHECK
    assert derive_state("ChampSelect") == AutomationState.CHAMP_SELECT
    assert derive_state("InProgress") == AutomationState.IN_GAME
    assert derive_state("EndOfGame") == AutomationState.SETTLEMENT



# ============================================================
# FIX-2: verify mismatch → rejected / queue no repeat POST
# ============================================================

def test_verify_mismatch_adds_rejected():
    lcu = FakeLcu()
    ev, on = make_events()
    stop = threading.Event()
    ex = ChampSelectActionExecutor(lambda m, e, d=None: lcu.request_raw(m, e, d), stop, on)
    ctrl = ChampSelectController(lambda m, e, d=None: lcu.request_raw(m, e, d), ex, stop, on)
    pending = PendingAction(action_id=9, action_type="pick", champion_id=157, actor_cell_id=3)
    ctrl._pending[9] = pending
    ctrl._rejected[9] = {22}  # 已有 rejected 必须保留
    ctrl._verify_completed(
        {"id": 9, "type": "pick", "actorCellId": 3, "championId": 99, "completed": True},
        pending,
    )
    assert 9 not in ctrl._pending
    assert 22 in ctrl._rejected.get(9, set())
    assert 157 in ctrl._rejected.get(9, set())
    assert 9 not in ctrl._no_candidate_actions
    assert any(c == "automation.action_changed" for c, _ in ev)


def test_toggle_starts_worker_if_not_running():
    lcu = FakeLcu()
    cfg = AutomationConfig(enabled=False, auto_accept=False)
    svc = LoLAutomationService(lcu=lcu, config=cfg)
    assert svc.running is False
    svc.toggle_auto_accept()
    assert svc.config.enabled is True
    assert svc.config.auto_accept is True
    assert svc.running is True
    svc.stop()


def test_queue_no_repeat_post():
    lcu = FakeLcu()
    lcu.route("GET", "/lol-lobby/v2/lobby/matchmaking/search-state", 200, {"isCurrentlyInQueue": False})
    lcu.route("POST", "/lol-lobby/v2/lobby/matchmaking/search", 204, {})
    ev, on = make_events()
    q = QueueController(lambda m, e, d=None: lcu.request_raw(m, e, d), on)
    r1 = q.try_start("Lobby")
    r2 = q.try_start("Lobby")
    posts = [c for c in lcu.calls if c[0] == "POST"]
    assert r1 is True
    assert r2 is False
    assert len(posts) == 1


# ============================================================
# A1.0: Auto Queue state-validation + post-verification (Q1-Q8)
# ============================================================


class _SearchFake:
    """search-state 随 POST 自动翻转为 True；可强制 POST 失败。"""
    def __init__(self, post_status=204):
        self.calls = []
        self.connected = True
        self.gameflow_phase = "Lobby"
        self.in_queue = False
        self.post_status = post_status
        self.flip_on_post = True

    def request_raw(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if endpoint == "/lol-lobby/v2/lobby/matchmaking/search-state":
            return 200, {"isCurrentlyInQueue": self.in_queue}
        if method == "POST" and endpoint == "/lol-lobby/v2/lobby/matchmaking/search":
            if self.flip_on_post:
                self.in_queue = True
            return self.post_status, None
        return 0, None

    def get_gameflow_phase(self):
        return self.gameflow_phase


def _queue_svc(cfg, lcu):
    from app.lol.automation.service import LoLAutomationService
    return LoLAutomationService(lcu=lcu, config=cfg)


def test_q1_queue_disabled_no_post():
    lcu = _SearchFake()
    svc = _queue_svc(AutomationConfig(enabled=True, auto_queue=False), lcu)
    svc._tick()
    posts = [c for c in lcu.calls if c[0] == "POST"]
    assert posts == []


def test_q2_lobby_starts_and_verifies():
    lcu = _SearchFake()
    q = QueueController(lcu.request_raw, make_events()[1])
    assert q.try_start("Lobby") is True
    assert lcu.in_queue is True
    assert any(c[0] == "POST" for c in lcu.calls)


def test_q3_already_searching_no_repeat_post():
    lcu = _SearchFake()
    lcu.in_queue = True
    q = QueueController(lcu.request_raw, make_events()[1])
    assert q.try_start("Lobby") is False
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_q4_readycheck_no_queue_post():
    lcu = _SearchFake()
    q = QueueController(lcu.request_raw, make_events()[1])
    q.try_start("ReadyCheck")
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_q5_champselect_no_queue_post():
    lcu = _SearchFake()
    q = QueueController(lcu.request_raw, make_events()[1])
    q.try_start("ChampSelect")
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_q6_inprogress_no_queue_post():
    lcu = _SearchFake()
    q = QueueController(lcu.request_raw, make_events()[1])
    q.try_start("InProgress")
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_q7_post_failure_records_error():
    lcu = _SearchFake(post_status=500)
    ev, on = make_events()
    q = QueueController(lcu.request_raw, on)
    assert q.try_start("Lobby") is True
    assert any(c == "automation.error" for c, _ in ev)


def test_q8_post_success_but_no_state_verify_failed():
    lcu = _SearchFake()
    lcu.flip_on_post = False  # POST 204 但状态没翻
    ev, on = make_events()
    q = QueueController(lcu.request_raw, on)
    q.try_start("Lobby")
    assert any(c == "automation.queue_search_verify_failed" for c, _ in ev)


# ============================================================
# A1.0: Auto Accept (A1-A8)
# ============================================================

class _ReadyFake:
    def __init__(self, accept_status=204, accept_leaves=True):
        self.calls = []
        self.connected = True
        self.gameflow_phase = "ReadyCheck"
        self.state = "InProgress"
        self.response = "None"
        self.accept_status = accept_status
        self.accept_leaves = accept_leaves

    def request_raw(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if endpoint == "/lol-matchmaking/v1/ready-check":
            return 200, {"state": self.state, "playerResponse": self.response, "readyCheckId": 1}
        if method == "POST" and endpoint == "/lol-matchmaking/v1/ready-check/accept":
            if self.accept_leaves:
                self.state = "None"
            return self.accept_status, None
        return 0, None

    def get_gameflow_phase(self):
        return self.gameflow_phase


def test_a1_accept_disabled_no_post():
    lcu = _ReadyFake()
    svc = _queue_svc(AutomationConfig(enabled=True, auto_accept=False), lcu)
    svc._tick()
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_a2_non_readycheck_no_post():
    lcu = _ReadyFake()
    lcu.state = "None"
    ev, on = make_events()
    rc = ReadyCheckController(lcu.request_raw, threading.Event(), on)
    rc.check(0.0)
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_a3_readycheck_delay_then_accept():
    lcu = _ReadyFake()
    ev, on = make_events()
    rc = ReadyCheckController(lcu.request_raw, threading.Event(), on)
    rc.check(0.0)
    assert any(c[0] == "POST" and c[1].endswith("/accept") for c in lcu.calls)
    assert any(c == "automation.accept_verified" for c, _ in ev)


def test_a4_delay_stop_no_post():
    lcu = _ReadyFake()
    ev, on = make_events()
    stop = threading.Event()
    rc = ReadyCheckController(lcu.request_raw, stop, on)
    import time
    def run(): rc.check(0.5)
    t = threading.Thread(target=run); t.start()
    stop.set(); t.join(timeout=3)
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_a5_state_changed_during_delay_no_post():
    lcu = _ReadyFake()
    # 第二次 GET（复核时）state 变 None
    n = {"k": 0}
    orig = lcu.request_raw
    def patched(method, endpoint, data=None):
        if endpoint == "/lol-matchmaking/v1/ready-check":
            n["k"] += 1
            if n["k"] >= 2:
                lcu.state = "None"
        return orig(method, endpoint, data)
    ev, on = make_events()
    rc = ReadyCheckController(patched, threading.Event(), on)
    rc.check(0.0)
    assert not any(c[0] == "POST" for c in lcu.calls)


def test_a6_accept_post_failure_records_error():
    lcu = _ReadyFake(accept_status=500, accept_leaves=False)
    ev, on = make_events()
    rc = ReadyCheckController(lcu.request_raw, threading.Event(), on)
    rc.check(0.0)
    assert any(c == "automation.error" for c, _ in ev)


def test_a7_accept_success_verifies_left_inprogress():
    lcu = _ReadyFake()
    ev, on = make_events()
    rc = ReadyCheckController(lcu.request_raw, threading.Event(), on)
    rc.check(0.0)
    assert any(c == "automation.accept_verified" for c, _ in ev)


def test_a8_worker_reentry_guard():
    lcu = _ReadyFake()
    ev, on = make_events()
    rc = ReadyCheckController(lcu.request_raw, threading.Event(), on)
    rc._active = True  # 模拟上一次尚未结束
    rc.check(0.0)
    assert not any(c[0] == "POST" for c in lcu.calls)
