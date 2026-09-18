"""Objective 数据层与状态机测试（无 pytest，直接 py 运行）。"""
import sys, types, os
sys.path.insert(0, os.path.abspath("."))

# stub 缺失依赖，与 cooldown 测试一致
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


def main():
    rules = load_ruleset()
    check("ruleset loaded, has dragon/baron/void_grubs/elder",
          all(rules.get(t) for t in ("dragon", "baron", "void_grubs", "elder")))
    check("ruleset carries patch metadata", bool(rules.patch))

    tr = ObjectiveTracker(rules)
    tr.on_game_start()
    ns = tr.next_spawn_times()
    check("first spawn: dragon=300, baron=1200, void_grubs=480",
          ns.get("dragon") == 300 and ns.get("baron") == 1200 and ns.get("void_grubs") == 480)
    check("elder not active before soul", ns.get("elder") is None)
    check("void_grubs one_per_game", rules.get("void_grubs").one_per_game is True)

    # 小龙 300 击杀 -> 下一条 600
    tr.on_dragon_killed(300.0, "Fire")
    check("dragon respawn = 600 after kill@300",
          tr.next_spawn_times().get("dragon") == 600.0)

    # 第 4 条龙 @900 -> soul -> elder 首次 1260
    tr.on_dragon_killed(450.0, "Fire")
    tr.on_dragon_killed(600.0, "Fire")
    tr.on_dragon_killed(750.0, "Fire")
    check("elder scheduled after 4th dragon",
          abs(tr.next_spawn_times().get("elder") - 1110.0) < 1.0)

    # elder 击杀 -> 下一条 elder
    tr.on_dragon_killed(1110.0, "Elder")
    check("elder respawn after elder kill",
          abs(tr.next_spawn_times().get("elder") - 1470.0) < 1.0)

    # baron 击杀 -> +360
    tr.on_baron_killed(1200.0)
    check("baron respawn = 1560", tr.next_spawn_times().get("baron") == 1560.0)

    # herald killed -> grubs done
    tr.on_herald_killed(500.0)
    check("void_grubs cleared after herald kill", "void_grubs" not in tr.next_spawn_times())

    # 跨局重置
    tr.on_game_end()
    check("game ended -> states GAME_ENDED",
          all(o["state"] == ObjectiveState.GAME_ENDED.value for o in tr.snapshot()))
    tr.on_game_start()
    ns2 = tr.next_spawn_times()
    check("new game resets timers (dragon=300 again)",
          ns2.get("dragon") == 300 and "elder" not in ns2)

    # summoner spell data versioning
    cat = SummonerSpellCatalog()
    f = cat.get_by_name("闪现")
    check("flash cooldown=300 from data", f and abs(f.cooldown - 300.0) < 0.1)
    check("catalog carries data_version", bool(cat.data_version))

    failed = [n for n, ok in PASS if not ok]
    print(f"\n{len(PASS)-len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
