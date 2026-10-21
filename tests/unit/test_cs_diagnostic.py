"""Phase 1A-R2: three-source diagnostic state machine tests."""
import pytest

pytestmark = pytest.mark.unit

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.lol.ongoing.diagnostic import ChampSelectDiagnostic


def classify(phase, cs, gfs=200):
    return ChampSelectDiagnostic._classify(phase, cs, gfs)


def test_classify():
    assert classify("ChampSelect", 200) == "CASE_A_NORMAL"
    assert classify("InProgress", 200) == "CASE_B_CONFLICT"
    assert classify("InProgress", 404, 200) == "CASE_C_NO_CS_API"
    assert classify("GameStart", 404, 200) == "CASE_F_TRANSITION"
    assert classify("ChampSelect", 404) == "CASE_E_CONFLICT"
    assert classify("Lobby", 200) == "CASE_D_CONFLICT"
    print("6/6 classify PASS")


def make(phase, cs_status, n=0):
    def req(method, endpoint, data=None):
        if "gameflow-phase" in endpoint:
            return 200, f'"{phase}"'
        if "v1/session" in endpoint and "champ-select" not in endpoint:
            return 200, {"gameData": {"gameId": 123, "queue": {"id": 430}, "gameMode": "CLASSIC"}}
        if "champ-select" in endpoint:
            if cs_status != 200:
                return cs_status, None
            players = [{"puuid": f"p{i}", "name": f"N{i}"} for i in range(n)]
            return 200, {"myTeam": players, "theirTeam": []}
        return 404, None
    return req


def test_tick():
    d = ChampSelectDiagnostic(make("InProgress", 404), interval=0.01)
    d._tick()
    e = d.ring()[-1]
    assert e["gfs_status"] == 200 and e["game_id"] == 123 and e["queue_id"] == 430
    assert e["case"] == "CASE_C_NO_CS_API"
    # transition timeline recorded
    d2 = ChampSelectDiagnostic(make("ChampSelect", 200, 10), interval=0.01)
    d2._tick()
    e2 = d2.ring()[-1]
    assert e2["unique_puuid"] == 10 and e2["case"] == "CASE_A_NORMAL"
    print("tick PASS")


if __name__ == "__main__":
    test_classify()
    test_tick()
    print("ALL PASS")
