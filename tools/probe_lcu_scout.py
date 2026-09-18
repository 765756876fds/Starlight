"""LCU-Scout-01: 腾讯国服 LCU 房间玩家与历史数据可行性探针

独立运行，不修改生产代码，不写 player.db。
用法:
  python tools/probe_lcu_scout.py
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import ssl
import base64

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _read_lockfile():
    """从 LeagueClient.exe 命令行或 lockfile 读 LCU 连接信息"""
    # 方式1: 进程命令行（项目 runtime 已用）
    import subprocess
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='LeagueClient.exe'",
             "get", "CommandLine"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "--app-port=" in line:
                port = line.split("--app-port=")[1].split()[0]
                token = line.split("--remoting-auth-token=")[1].split()[0]
                return int(port), token
    except Exception:
        pass
    return None, None


def _get(port, token, path):
    url = f"https://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"riot:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Accept", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            body = r.read().decode("utf-8")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return -1, str(e)


def probe_current_summoner(port, token):
    print("\n=== A. Current Summoner ===")
    path = "/lol-summoner/v1/current-summoner"
    status, body = _get(port, token, path)
    print(f"URL: https://127.0.0.1:{port}{path}")
    print(f"HTTP: {status}")
    if status == 200:
        d = json.loads(body)
        print(f"puuid: {d.get('puuid')}")
        print(f"summonerId: {d.get('summonerId')}")
        print(f"gameName: {d.get('gameName')}")
        print(f"tagLine: {d.get('tagLine')}")
        print(f"displayName: {d.get('displayName')}")
        print(f"原始字段: {list(d.keys())}")
        return d
    else:
        print(f"FAIL body: {body[:200]}")
        return None


def probe_lobby(port, token):
    print("\n=== B. Lobby Members ===")
    path = "/lol-lobby/v2/lobby/members"
    status, body = _get(port, token, path)
    print(f"URL: ...{path}")
    print(f"HTTP: {status}")
    members = []
    if status == 200:
        members = json.loads(body)
        print(f"成员数量: {len(members)}")
        for i, m in enumerate(members):
            print(f"\n--- member[{i}] ---")
            print(f"  字段: {list(m.keys())}")
            summ = m.get("summoner") or {}
            print(f"  summoner.puuid: {summ.get('puuid')}")
            print(f"  summoner.summonerId: {summ.get('summonerId')}")
            print(f"  summoner.gameName: {summ.get('gameName')}")
            print(f"  summoner.tagLine: {summ.get('tagLine')}")
            print(f"  summoner.displayName: {summ.get('displayName')}")
    else:
        print(f"FAIL body: {body[:200]}")
    return members


def probe_match_history(port, token, puuid, count=20):
    print(f"\n=== C. Match History for {puuid[:8]}... ===")
    path = f"/lol-match-history/v1/products/lol/{puuid}/matches"
    status, body = _get(port, token, path)
    print(f"URL: ...{path}")
    print(f"HTTP: {status}")
    games = []
    if status == 200:
        d = json.loads(body)
        games = d.get("games", {}).get("games", [])
        print(f"返回比赛数量: {len(games)}")
        for i, g in enumerate(games[:count]):
            print(f"\n--- game[{i}] ---")
            print(f"  gameId: {g.get('gameId')}")
            print(f"  gameCreation: {g.get('gameCreation')}")
            print(f"  gameDuration: {g.get('gameDuration')}")
            print(f"  mapId: {g.get('mapId')}")
            print(f"  gameMode: {g.get('gameMode')}")
            # participant stats (first participant = self)
            participants = g.get("participants", [])
            if participants:
                p = participants[0]
                print(f"  championId: {p.get('championId')}")
                stats = p.get("stats", {})
                print(f"  win: {stats.get('win')}")
                print(f"  kills: {stats.get('kills')}")
                print(f"  deaths: {stats.get('deaths')}")
                print(f"  assists: {stats.get('assists')}")
                print(f"  position: {p.get('position')}")
                print(f"  role: {p.get('role')}")
                print(f"  participant 字段: {list(p.keys())}")
    else:
        print(f"FAIL body: {body[:200]}")
    return games


def probe_champ_select(port, token):
    print("\n=== D. Champion Select ===")
    path = "/lol-champ-select/v1/session"
    status, body = _get(port, token, path)
    print(f"URL: ...{path}")
    print(f"HTTP: {status}")
    if status == 200:
        d = json.loads(body)
        my_team = d.get("myTeam", [])
        their_team = d.get("theirTeam", [])
        print(f"myTeam 数量: {len(my_team)}")
        print(f"theirTeam 数量: {len(their_team)}")
        for label, team in [("myTeam", my_team), ("theirTeam", their_team)]:
            for i, p in enumerate(team):
                print(f"\n--- {label}[{i}] ---")
                print(f"  字段: {list(p.keys())}")
                print(f"  puuid: {p.get('puuid')}")
                print(f"  summonerId: {p.get('summonerId')}")
                print(f"  gameName: {p.get('gameName')}")
                print(f"  tagLine: {p.get('tagLine')}")
                print(f"  cellId: {p.get('cellId')}")
                print(f"  championId: {p.get('championId')}")
                print(f"  assignedPosition: {p.get('assignedPosition')}")
        return d
    else:
        print(f"FAIL body: {body[:200]}")
        return None


def main():
    port, token = _read_lockfile()
    if not port:
        print("无法读取 LCU 连接信息，请先启动 LoL Client")
        return
    print(f"LCU: port={port}")

    summ = probe_current_summoner(port, token)
    members = probe_lobby(port, token)

    puuid = (summ or {}).get("puuid")
    if puuid:
        probe_match_history(port, token, puuid)

    probe_champ_select(port, token)


if __name__ == "__main__":
    main()
