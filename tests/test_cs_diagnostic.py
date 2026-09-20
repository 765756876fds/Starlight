"""Phase 1A-R1: champ-select diagnostic state machine tests."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.lol.ongoing.diagnostic import ChampSelectDiagnostic


def make(phase, cs_status, n=0):
    def req(method, endpoint, data=None):
        if "gameflow-phase" in endpoint:
            return 200, f'"{phase}"'
        if "champ-select" in endpoint:
            if cs_status != 200:
                return cs_status, None
            players = [{"puuid": f"p{i}", "name": f"N{i}", "team": "myTeam"} for i in range(n)]
            return 200, {"myTeam": players, "theirTeam": []}
        return 404, None
    return req


def run_case(phase, cs_status, n):
    d = ChampSelectDiagnostic(make(phase, cs_status, n), interval=0.01)
    d._tick()
    ring = d.ring()
    last = ring[-1]
    return last


def test_cases():
    a = run_case("ChampSelect", 200, 10)
    assert a["gameflow_phase"] == "ChampSelect" and a["cs_status"] == 200
    b = run_case("InProgress", 200, 4)
    assert b["cs_status"] == 200 and b["player_count"] == 4
    c = run_case("InProgress", 404, 0)
    assert c["cs_status"] == 404
    dd = run_case("Lobby", 404, 0)
    assert dd["cs_status"] == 404
    e = run_case("ChampSelect", 200, 4)
    assert e["player_count"] == 4 and e["unique_puuid"] == 4
    f = run_case("ChampSelect", 200, 10)
    assert f["unique_puuid"] == 10
    # duplicate puuid: 10 cells but 9 unique
    def req(method, endpoint, data=None):
        if "gameflow" in endpoint: return 200, '"ChampSelect"'
        players = [{"puuid": "dup", "name": "x"}, {"puuid": "dup", "name": "y"}] + [{"puuid": f"p{i}", "name": f"N{i}"} for i in range(8)]
        return 200, {"myTeam": players, "theirTeam": []}
    d = ChampSelectDiagnostic(req, interval=0.01)
    d._tick(); last = d.ring()[-1]
    assert last["player_count"] == 10 and last["unique_puuid"] == 9
    print("7/7 PASS")


if __name__ == "__main__":
    test_cases()
