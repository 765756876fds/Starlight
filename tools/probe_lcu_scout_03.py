"""LCU Scout 03: 真实国服 Champion Select myTeam -> 最近20场验证.

只读 probe，不修改生产项目。
运行：在进入 Champ Select 后执行
    python tools/probe_lcu_scout_03.py
输出：tools/output/teammate_*.json
"""
import base64
import json
import os
import sys
import time
from datetime import datetime

import requests
import urllib3

urllib3.disable_warnings()

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)


def discover_lcu():
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        if p.info["name"] != "LeagueClientUx.exe":
            continue
        cmd = p.info["cmdline"] or []
        port = token = None
        for a in cmd:
            if a.startswith("--app-port="):
                port = int(a.split("=", 1)[1])
            elif a.startswith("--remoting-auth-token="):
                token = a.split("=", 1)[1]
        if port and token:
            return port, token
    raise RuntimeError("LeagueClientUx not found")


def main():
    port, token = discover_lcu()
    base = f"https://127.0.0.1:{port}"
    s = requests.Session()
    s.verify = False
    s.auth = ("riot", token)

    # 1. champ select session
    r = s.get(f"{base}/lol-champ-select/v1/session", timeout=10)
    print("[CS] status", r.status_code)
    if r.status_code != 200:
        print("Not in champ select. Phase check:")
        r2 = s.get(f"{base}/lol-gameflow/v1/gameflow-phase", timeout=5)
        print("[phase]", r2.status_code, r2.text)
        return 1

    cs = r.json()
    with open(os.path.join(OUT_DIR, "champ_select_session.json"), "w", encoding="utf-8") as f:
        json.dump(cs, f, ensure_ascii=False, indent=2)

    local_id = cs.get("localPlayerCellId")
    my_team = cs.get("myTeam", [])
    print(f"[CS] gameId={cs.get('gameId')} queueId={cs.get('queueId')} localCell={local_id} myTeam={len(my_team)}")

    teammates = []
    self_puuid = None
    for m in my_team:
        cell = m.get("cellId")
        is_self = (cell == local_id)
        puuid = m.get("puuid") or ""
        row = {
            "cellId": cell,
            "is_self": is_self,
            "gameName": m.get("gameName"),
            "tagLine": m.get("tagLine"),
            "puuid": puuid,
            "summonerId": m.get("summonerId"),
            "championId": m.get("championId"),
            "assignedPosition": m.get("assignedPosition"),
            "selectedPosition": m.get("selectedPosition"),
        }
        print(f"  [{'SELF' if is_self else 'TEAM'}] cell={cell} {m.get('gameName')}#{m.get('tagLine')} puuid={puuid[:8] if puuid else '(empty)'}...")
        if is_self:
            self_puuid = puuid
        elif puuid:
            teammates.append(row)

    print(f"[TEAMMATES] {len(teammates)} with PUUID")

    # 2. for each teammate, fetch match history
    summary = {
        "gameId": cs.get("gameId"),
        "queueId": cs.get("queueId"),
        "self_puuid": self_puuid,
        "teammates": [],
    }
    for i, t in enumerate(teammates, 1):
        puuid = t["puuid"]
        print(f"\n[HIST] {i} {t['gameName']}#{t['tagLine']} puuid={puuid[:8]}...")
        hr = s.get(
            f"{base}/lol-match-history/v1/products/lol/{puuid}/matches",
            params={"startIndex": 0, "count": 20},
            timeout=15,
        )
        print(f"  status={hr.status_code}")
        with open(os.path.join(OUT_DIR, f"teammate_{i}_history.json"), "w", encoding="utf-8") as f:
            json.dump(hr.json(), f, ensure_ascii=False, indent=2)
        if hr.status_code != 200:
            t["http_status"] = hr.status_code
            continue
        data = hr.json()
        games = data.get("games", {}).get("games", [])
        t["http_status"] = 200
        t["returned_count"] = len(games)
        if games:
            t["newest_gameCreation"] = games[0].get("gameCreation")
            t["oldest_gameCreation"] = games[-1].get("gameCreation")
        t["recent3"] = []
        for g in games[:3]:
            st = g.get("stats", {})
            t["recent3"].append({
                "gameId": g.get("gameId"),
                "gameCreation": g.get("gameCreation"),
                "gameCreationDate": g.get("gameCreationDate"),
                "queueId": g.get("queueId"),
                "gameMode": g.get("gameMode"),
                "championId": g.get("championId"),
                "win": st.get("win"),
                "kills": st.get("kills"),
                "deaths": st.get("deaths"),
                "assists": st.get("assists"),
            })
        wins = sum(1 for g in games if g.get("stats", {}).get("win"))
        t["wins"] = wins
        t["losses"] = len(games) - wins
    summary["teammates"] = teammates
    with open(os.path.join(OUT_DIR, "lcu_scout_03_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
