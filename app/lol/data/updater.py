"""数据更新器：尝试从 CommunityDragon / Meraki 拉取；失败时回退内置静态表。

Phase B 阶段不强制联网：updater 返回 True/False 表示是否成功刷新缓存，
调用方（Catalog）应在拉取失败时使用内置数据。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import cache as cache_mod
from .cache import DEFAULT_CACHE_DIR

COMMUNITYDRAGON_BASE = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/zh_cn/v1/"
)
MERAKI_BASE = "https://raw.githubusercontent.com/MerakiAnalytics/lolstaticdata/main/"


def update(cache_dir: Path = DEFAULT_CACHE_DIR, timeout: float = 3.0) -> bool:
    """尝试拉取远程数据。网络失败返回 False（调用方用内置数据）。"""
    try:
        import urllib.request  # noqa: WPS433
        url = COMMUNITYDRAGON_BASE + "champion-summary.json"
        req = urllib.request.Request(url, headers={"User-Agent": "lol-music-agent"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec
            payload = json.loads(resp.read().decode("utf-8"))
        cache_mod.save_json(cache_dir, "champions_remote.json",
                            {"items": payload})
        return True
    except Exception:
        return False
