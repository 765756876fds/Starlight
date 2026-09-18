"""一次性脚本：从 CommunityDragon 拉取全量英雄 + 技能 CD 到 data/lol_data/。

运行：python tools/update_lol_data.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = ("https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
        "global/zh_cn/v1/")
OUT = ROOT / "data" / "lol_data"
OUT.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "lol-music-agent-updater"}


def fetch(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print("Fetching champion-summary.json ...")
    summary = fetch(BASE + "champion-summary.json")
    champions = []
    for c in summary:
        cid = int(c.get("id", -1))
        if cid <= 0:
            continue
        champions.append({
            "id": cid,
            "name": c.get("description") or c.get("name") or "",  # 短名（亚索）
            "title": c.get("name") or "",                          # 称号（疾风剑豪）
            "alias": c.get("alias") or "",
            "icon": c.get("squarePortraitPath") or "",
            "roles": c.get("roles") or [],
        })
    print(f"  champions: {len(champions)}")

    # 逐英雄拉 spells（带重试）
    abilities = []
    failed = []
    for i, ch in enumerate(champions):
        cid = ch["id"]
        d = None
        last_err = None
        for attempt in range(3):
            try:
                d = fetch(BASE + f"champions/{cid}.json", timeout=8.0)
                break
            except Exception as e:
                last_err = e
                time.sleep(0.3 * (attempt + 1))
        if d is None:
            failed.append((cid, str(last_err)))
            continue
        for sp in d.get("spells", []):
            key = (sp.get("spellKey") or "").upper()
            if key not in ("Q", "W", "E", "R"):
                continue
            coeffs = sp.get("cooldownCoefficients") or [0.0]
            cd = float(coeffs[0]) if coeffs else 0.0
            abilities.append({
                "champion_id": cid,
                "slot": key,
                "name": sp.get("name") or "",
                "cooldown": cd,
                "cooldown_by_level": [float(x) for x in coeffs],
            })
        if (i + 1) % 50 == 0:
            print(f"  spells progress: {i+1}/{len(champions)}, abilities={len(abilities)}")
        time.sleep(0.05)  # 温和

    print(f"  abilities: {len(abilities)}, failed: {len(failed)}")

    # 写文件
    def save(name, data):
        p = OUT / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        print(f"  wrote {p} ({p.stat().st_size//1024} KB)")

    save("champions.json", champions)
    save("abilities.json", abilities)
    save("version.json", {
        "patch": "cd-latest",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "champion_count": len(champions),
        "ability_count": len(abilities),
    })
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
