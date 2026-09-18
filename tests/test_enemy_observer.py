"""敌方观察层测试（无 pytest，直接 py 运行）。"""
import sys, types, os
sys.path.insert(0, os.path.abspath("."))

_urllib3 = types.ModuleType("urllib3")
_urllib3.disable_warnings = lambda *a, **k: None
_urllib3.exceptions = types.SimpleNamespace(InsecureRequestWarning=type("InsecureRequestWarning", (Exception,), {}))
sys.modules["urllib3"] = _urllib3
sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("yaml", types.ModuleType("yaml"))
_ws = types.ModuleType("websocket")
_ws.create_connection = lambda *a, **k: None
_ws.WebSocketConnectionClosedException = type("WebSocketConnectionClosedException", (Exception,), {})
sys.modules["websocket"] = _ws

from app.lol.snapshot import GameSnapshot, PlayerInfo
from app.lol.enemy import (
    EnemyRoster, ability_cast_support, should_auto_track_summoner_spells,
    CastSupport,
)

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def mk_player(name, champ, cid, team, level, spells=None, items=None):
    return PlayerInfo(
        summoner_name=name, champion_name=champ, champion_id=cid,
        team=team, level=level,
        summoner_spells=spells or [], items=items or [],
    )


def main():
    snap = GameSnapshot(
        game_time=600.0, is_game_running=True,
        ally_team_name="ORDER", enemy_team_name="CHAOS",
        players=[],
    )
    s = [{"displayName": "SummonerFlash", "cooldownRemaining": 0.0},
         {"displayName": "SummonerHaste", "cooldownRemaining": 0.0}]
    it = [{"itemID": 3157}, {"itemID": 6656}]
    snap.enemy_team = [
        mk_player("EnemyMid", "Syndra", 134, "CHAOS", 12, s, it),
        mk_player("EnemyTop", "Malphite", 54, "CHAOS", 11, s, []),
    ]

    roster = EnemyRoster()
    profs = roster.build(snap)
    check("roster builds 2 enemies", len(profs) == 2)
    syn = roster.find(snap, "Syndra")
    check("find by name (case-insensitive)", syn is not None and syn.champion_id == 134)
    mal = roster.find(snap, "54")
    check("find by id", mal is not None and mal.champion_name == "Malphite")
    check("enemy summoner spells parsed", "SummonerFlash" in syn.summoner_spells)
    check("enemy items parsed (itemID list)", 3157 in syn.items)
    check("haste=0, source=unavailable (no guessing)",
          syn.ability_haste == 0.0 and syn.haste_source == "unavailable")
    check("empty champion -> find None", roster.find(snap, "") is None)

    # 能力二封口：2999 不提供 Q/W/E/R cast
    check("ability cast = NOT_SUPPORTED",
          ability_cast_support() == CastSupport.NOT_SUPPORTED)
    check("summoner spell auto tracking disabled by policy",
          should_auto_track_summoner_spells() is False)

    # 第二局：roster 无跨局残留（纯函数，每次 build 新对象）
    snap2 = GameSnapshot(game_time=10.0, is_game_running=True,
                         enemy_team=[mk_player("EnemyMid2", "Ahri", 103, "CHAOS", 1)])
    check("new game roster does not inherit old enemies",
          len(EnemyRoster().build(snap2)) == 1)

    failed = [n for n, ok in PASS if not ok]
    print(f"\n{len(PASS)-len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
