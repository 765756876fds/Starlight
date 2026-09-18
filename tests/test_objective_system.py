"""Objective 数据层与状态机测试（无 pytest，直接 py 运行）。"""
import sys, types, os
sys.path.insert(0, os.path.abspath("."))

# stub 缺失依赖
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

from app.lol.objectives import load_ruleset, ObjectiveTracker, ObjectiveState
from app.lol.data.summoner_spell_catalog import SummonerSpellCatalog

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def fresh():
    return ObjectiveTracker(load_ruleset())


def main():
    rules = load_ruleset()
    check("ruleset has dragon/baron/void_grubs/elder",
          all(rules.get(t) for t in ("dragon", "baron", "void_grubs", "elder")))

    # 1) first spawn
    tr = fresh(); tr.on_game_start()
    ns = tr.next_spawn_times()
    check("first spawn dragon=300 baron=1200 grubs=480",
          ns.get("dragon") == 300 and ns.get("baron") == 1200 and ns.get("void_grubs") == 480)
    check("elder inactive before soul", "elder" not in ns)

    # 2) dragon respawn (team A)
    tr.on_dragon_killed(300.0, "Fire", "order")
    check("dragon respawn = 600", tr.next_spawn_times().get("dragon") == 600.0)

    # 3-6) soul by TEAM
    # Case A: A=4,B=0 -> elder
    tr = fresh(); tr.on_game_start()
    for t in (300, 450, 600, 750):
        tr.on_dragon_killed(t, "Fire", "order")
    check("Case A A=4,B=0 -> elder @1110",
          abs(tr.next_spawn_times().get("elder") - 1110.0) < 1.0)

    # Case B: A=3,B=1 -> no elder
    tr = fresh(); tr.on_game_start()
    for t in (300, 450, 600):
        tr.on_dragon_killed(t, "Fire", "order")
    tr.on_dragon_killed(700, "Fire", "chaos")
    check("Case B A=3,B=1 -> no elder", "elder" not in tr.next_spawn_times())

    # Case C: A=2,B=2 -> no elder
    tr = fresh(); tr.on_game_start()
    for t, team in ((300, "order"), (400, "chaos"), (500, "order"), (600, "chaos")):
        tr.on_dragon_killed(t, "Fire", team)
    check("Case C A=2,B=2 -> no elder", "elder" not in tr.next_spawn_times())

    # Case D: A=3,B=2 -> no elder
    tr = fresh(); tr.on_game_start()
    for t, team in ((300, "order"), (400, "order"), (500, "chaos"),
                    (600, "order"), (700, "chaos")):
        tr.on_dragon_killed(t, "Fire", team)
    check("Case D A=3,B=2 -> no elder", "elder" not in tr.next_spawn_times())

    # Case E: A=4 -> elder +6min(360)
    tr = fresh(); tr.on_game_start()
    for t in (300, 450, 600, 900):
        tr.on_dragon_killed(t, "Fire", "order")
    check("Case E A=4 -> elder@1260",
          abs(tr.next_spawn_times().get("elder") - 1260.0) < 1.0)

    # Case F: elder active -> later normal dragon does NOT recreate elder
    tr.on_dragon_killed(1000, "Fire", "order")  # order now 5, elder already set
    check("Case F later dragon does not reset elder",
          abs(tr.next_spawn_times().get("elder") - 1260.0) < 1.0)

    # unknown team must not trigger soul
    tr = fresh(); tr.on_game_start()
    for t in (300, 450, 600, 750):
        tr.on_dragon_killed(t, "Fire", "UNKNOWN")
    check("UNKNOWN team -> no elder", "elder" not in tr.next_spawn_times())

    # 7) baron respawn
    tr = fresh(); tr.on_game_start()
    tr.on_baron_killed(1200.0)
    check("baron respawn=1560", tr.next_spawn_times().get("baron") == 1560.0)

    # 8) grub one-shot; 9) herald
    tr.on_herald_killed(500.0)
    check("grubs cleared after herald kill", "void_grubs" not in tr.next_spawn_times())

    # elder kill -> respawn
    tr = fresh(); tr.on_game_start()
    for t in (300, 450, 600, 900):
        tr.on_dragon_killed(t, "Fire", "order")  # elder @1260
    tr.on_dragon_killed(1260.0, "Elder", "order")
    check("elder respawn after elder kill",
          abs(tr.next_spawn_times().get("elder") - 1620.0) < 1.0)

    # 12) game end cleanup; 13) second game reset
    tr.on_game_end()
    check("game end -> all GAME_ENDED",
          all(o["state"] == ObjectiveState.GAME_ENDED.value for o in tr.snapshot()))
    tr.on_game_start()
    ns2 = tr.next_spawn_times()
    check("game2 resets: dragon=300, no elder, counters cleared",
          ns2.get("dragon") == 300 and "elder" not in ns2 and ns2.get("baron") == 1200)
    # after reset, team counters empty -> 4 more needed
    for t in (300, 450, 600):
        tr.on_dragon_killed(t, "Fire", "order")
    check("game2 only 3 dragons -> no elder", "elder" not in tr.next_spawn_times())

    # summoner spell data versioning
    cat = SummonerSpellCatalog()
    f = cat.get_by_name("闪现")
    check("flash=300 from data", f and abs(f.cooldown - 300.0) < 0.1)

    failed = [n for n, ok in PASS if not ok]
    print(f"\n{len(PASS)-len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
