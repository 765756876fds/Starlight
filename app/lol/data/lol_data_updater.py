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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.core.logger import get_logger
from app.core.path_resolve import USER_DATA_DIR, PROJECT_ROOT, lol_data_dir
import sys as _sys

# 写入目录：frozen 态总是写到用户目录（用户更新后的数据）
# 读取方通过 lol_data_dir() 优先读用户目录，不存在回退到程序资源目录
if getattr(_sys, "frozen", False):
    OUT = USER_DATA_DIR / "data" / "lol_data"
else:
    OUT = PROJECT_ROOT / "data" / "lol_data"

logger = get_logger()

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


def update_champions_and_abilities(timeout: float = 10.0, progress_cb=None) -> dict[str, object]:
    """拉取英雄 + 技能 CD → champions.json / abilities.json / version.json。

    并发下载（ThreadPoolExecutor 10 worker）：逐英雄串行是"更新卡死"的根因——
    海外 CDN 下 170 英雄 × 3 次重试 × 8s 单请求上限可达数十分钟。并发 + 总时长
    护栏（120s）保证最坏情形可预期；progress_cb(done, total) 每完成一个英雄回调。

    返回 {"ok": True, "champions": N, "abilities": M, "failed": K}；
    网络/解析失败抛异常（旧文件保留）；英雄解析为空抛 ValueError。
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

    if len(champions) == 0:
        raise ValueError("解析出的英雄列表为空：CD 数据结构异常或返回为空")

    def _fetch_one(ch: dict):
        """单个英雄 + 技能下载（最多重试 3 次；内部捕获异常，绝不抛给线程池）。"""
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
            return ch, None, str(last_err)
        abls = []
        for sp in d.get("spells", []):
            key = (sp.get("spellKey") or "").upper()
            if key not in ("Q", "W", "E", "R"):
                continue
            coeffs = sp.get("cooldownCoefficients") or [0.0]
            abls.append({
                "champion_id": cid,
                "slot": key,
                "name": sp.get("name") or "",
                "cooldown": float(coeffs[0]) if coeffs else 0.0,
                "cooldown_by_level": [float(x) for x in coeffs],
            })
        time.sleep(0.02)  # 并发下的温和限速（10 worker 并行，足够防 CDN 风暴）
        return ch, abls, None

    abilities = []
    failed = []
    done = 0
    total = len(champions)
    deadline = time.monotonic() + 120.0  # 总时长护栏：整体超过 120s 即中断
    executor = ThreadPoolExecutor(max_workers=10)
    try:
        futs = {executor.submit(_fetch_one, ch): ch for ch in champions}
        for fut in as_completed(futs):
            if time.monotonic() > deadline:
                raise TimeoutError("下载总时长超限，请检查网络或代理")
            ch, abls, err = fut.result()
            done += 1
            if abls is None:
                failed.append((ch["id"], err))
                logger.warning(f"英雄 {ch['id']} 技能数据下载失败: {err}")
            else:
                abilities.extend(abls)
                if not abls:
                    logger.warning(f"英雄 {ch['id']} 技能数据为空或解析失败")
            if progress_cb is not None:
                progress_cb(done, total)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

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


def update_items(timeout: float = 10.0) -> dict[str, object]:
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


def update_all(timeout: float = 10.0, progress_cb=None) -> dict[str, object]:
    """英雄 + 技能 + 装备一次更新。任一步失败抛异常（旧文件保留）。

    progress_cb(done, total)：英雄/技能下载阶段的进度回调（每完成一个英雄调用）。
    """
    stats = update_champions_and_abilities(timeout, progress_cb=progress_cb)
    items = update_items(timeout)
    return {**stats, "items": items["items"]}
