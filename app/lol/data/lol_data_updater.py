"""LoL 静态数据一键更新（英雄 / 技能 CD / 装备中文名）。

数据源：
- 英雄 + 技能：CommunityDragon zh_cn（champion-summary.json + 逐英雄 champions/{id}.json spells）
- 装备：DataDragon zh_CN item.json（取最新版本号）

全部使用 tmp + os.replace 原子写：失败/中断不会留下半写文件，旧数据保持可用。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parent.parent.parent.parent
OUT = ROOT / "data" / "lol_data"

CD_BASE = ("https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
           "global/zh_cn/v1/")
DD_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DD_ITEM_URL = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/item.json"

HEADERS = {"User-Agent": "lol-music-agent-updater"}


def _fetch(url: str, timeout: float):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec
        return json.loads(r.read().decode("utf-8"))


def _save(name: str, data) -> None:
    """tmp + os.replace 原子写，保证不产生半写文件。"""
    p = OUT / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def update_champions_and_abilities(timeout: float = 10.0) -> Dict[str, object]:
    """拉取英雄 + 技能 CD → champions.json / abilities.json / version.json。

    返回 {"ok": True, "champions": N, "abilities": M, "failed": K}；网络/解析失败抛异常。
    """
    OUT.mkdir(parents=True, exist_ok=True)
    summary = _fetch(CD_BASE + "champion-summary.json", timeout)

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

    abilities = []
    failed = []
    for ch in champions:
        cid = ch["id"]
        d = None
        last_err = None
        for attempt in range(3):
            try:
                d = _fetch(CD_BASE + f"champions/{cid}.json", min(timeout, 8.0))
                break
            except Exception as e:  # noqa: BLE001
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
            abilities.append({
                "champion_id": cid,
                "slot": key,
                "name": sp.get("name") or "",
                "cooldown": float(coeffs[0]) if coeffs else 0.0,
                "cooldown_by_level": [float(x) for x in coeffs],
            })
        time.sleep(0.05)  # 温和限速，避免被 CDN 限流

    _save("champions.json", champions)
    _save("abilities.json", abilities)
    _save("version.json", {
        "patch": "cd-latest",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "champion_count": len(champions),
        "ability_count": len(abilities),
    })
    return {"ok": True, "champions": len(champions), "abilities": len(abilities),
            "failed": len(failed)}


def update_items(timeout: float = 10.0) -> Dict[str, object]:
    """拉取 DataDragon zh_CN item.json → items.json（id → 中文名）。

    注意：DataDragon 返回 data["data"] 为 dict，key 即装备 id（item 对象内无 id 字段）；
    name 可能带 HTML 标签（如 <rarityLegendary>…</rarityLegendary>），需清洗；空名跳过。
    """
    OUT.mkdir(parents=True, exist_ok=True)
    version = _fetch(DD_VERSIONS_URL, timeout)[0]
    data = _fetch(DD_ITEM_URL.format(ver=version), timeout)["data"]
    out = {}
    for k, v in data.items():
        name = re.sub(r"<[^>]+>", "", v.get("name") or "").strip()
        if name:
            out[k] = name
    payload = {
        "version": version,
        "source": "DataDragon zh_CN item.json",
        "items": dict(sorted(out.items(), key=lambda kv: int(kv[0]))),
    }
    _save("items.json", payload)
    return {"ok": True, "items": len(out)}


def update_all(timeout: float = 10.0) -> Dict[str, object]:
    """英雄 + 技能 + 装备一次更新。任一步失败抛异常（旧文件保留）。"""
    stats = update_champions_and_abilities(timeout)
    items = update_items(timeout)
    return {**stats, "items": items["items"]}
