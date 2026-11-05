"""Public film information, independent of every playback/source resolver."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import requests

from .library import atomic_json, title_key

logger = logging.getLogger(__name__)
CATEGORIES = {"all": "发现", "movie": "电影", "tv": "电视剧", "anime": "动漫", "variety": "综艺"}


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    url: str
    automatic: bool = False


PLATFORMS = (
    Platform("bilibili", "哔哩哔哩", "https://www.bilibili.com/v/popular/rank/bangumi", True),
    Platform("iqiyi", "爱奇艺", "https://www.iqiyi.com/ranks1PCW/home", True),
    Platform("tencent", "腾讯视频", "https://v.qq.com/ranks/pages/index.html", True),
    Platform("youku", "优酷", "https://www.youku.com/channel/webtv/list"),
    Platform("mango", "芒果TV", "https://www.mgtv.com/"),
)


class CatalogReadError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def fetch_public(url, *, text=False):
    """Bounded, credential-free reads; retain the user's network/proxy settings."""
    started = time.monotonic()
    with requests.get(url, headers={"User-Agent": "Starlight/0.1"}, timeout=(3, 6),
                      stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise CatalogReadError("service_rejected")
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 2_000_000:
                raise CatalogReadError("response")
            if time.monotonic() - started > 12:
                raise requests.Timeout("rank read budget exceeded")
        return content.decode("utf-8") if text else json.loads(content)


def official_url(url, platform):
    if not isinstance(url, str):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    domains = {"bilibili": "bilibili.com", "iqiyi": "iqiyi.com", "tencent": "v.qq.com",
               "youku": "youku.com", "mango": "mgtv.com"}
    domain, host = domains.get(platform), parsed.hostname or ""
    return url if parsed.scheme == "https" and domain and (host == domain or host.endswith("." + domain)) else ""


def merge_works(records):
    """Merge only a complete matching identity; unknown metadata stays separate."""
    out, identities = [], {}
    for record in records:
        if not isinstance(record, dict) or not record.get("title"):
            continue
        row = deepcopy(record)
        kind, year, region, season = row.get("kind"), row.get("year"), row.get("region"), row.get("season")
        complete = type(year) is int and 1800 <= year <= 2200 and bool(region) and kind in ("movie", "tv", "anime", "variety")
        if kind in ("tv", "anime", "variety") and type(season) is not int:
            complete = False
        identity = (title_key(row["title"]), year, kind, 0 if kind == "movie" else season,
                    title_key(region or "")) if complete else None
        provenance = {k: row.get(k) for k in ("platform", "platform_name", "rank", "url", "source_id", "board", "url_kind", "source_updated")}
        if identity is not None and identity in identities:
            target = identities[identity]
            if provenance not in target["sources"]:
                target["sources"].append(provenance)
            continue
        row["sources"] = [provenance]
        out.append(row)
        if identity is not None:
            identities[identity] = row
    return out


class BilibiliRank:
    """Read-only endpoints used by the public ranking page; no playback URL API."""
    key = "bilibili"
    name = "哔哩哔哩"
    categories = ("movie", "tv", "anime")
    ENDPOINTS = {
        "movie": "https://api.bilibili.com/pgc/season/rank/web/list?day=3&season_type=2",
        "tv": "https://api.bilibili.com/pgc/season/rank/web/list?day=3&season_type=5",
        "anime": "https://api.bilibili.com/pgc/web/rank/list?day=3&season_type=1",
    }

    def __init__(self, fetch=None):
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(url):
        return fetch_public(url)

    def read(self, category):
        payload = self.fetch(self.ENDPOINTS[category])
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise CatalogReadError("response")
        if payload["code"] != 0:
            raise CatalogReadError("service_rejected")
        data = payload.get("data") or payload.get("result")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError("ranking response schema changed")
        rows = []
        for index, item in enumerate(data["list"][:100]):
            if not isinstance(item, dict) or not item.get("title") or not item.get("season_id"):
                continue
            url = official_url(item.get("url"), self.key)
            if not url:
                continue
            rows.append({"title": str(item["title"])[:160], "kind": category,
                         "year": None, "region": None, "season": None,
                         "platform": self.key, "platform_name": self.name,
                         "source_id": str(item["season_id"]), "rank": item.get("rank", index + 1),
                         "url": url, "cover_url": self.cover_url(item.get("cover")),
                         "board": CATEGORIES[category], "note": str(data.get("note") or "")[:240]})
        if data["list"] and not rows:
            raise ValueError("ranking items could not be validated")
        return rows

    @staticmethod
    def cover_url(value):
        if not isinstance(value, str):
            return ""
        value = "https:" + value if value.startswith("//") else value
        if value.startswith("http://"):
            value = "https://" + value[7:]
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return value if parsed.scheme == "https" and (host == "hdslb.com" or host.endswith(".hdslb.com")) else ""


class MovieDiscovery:
    TTL = 1800

    def __init__(self, cache_path, providers=None, clock=time.time):
        from .rank_sources import IqiyiRank, TencentRank
        self.cache_path = Path(cache_path)
        self.providers = list(providers if providers is not None else [BilibiliRank(), IqiyiRank(), TencentRank()])
        self.clock = clock
        self._lock = threading.RLock()
        self._cache = {}
        self._generations = {}
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("version") == 1 and isinstance(cached.get("boards"), dict):
                self._cache = cached["boards"]
        except (OSError, ValueError, TypeError):
            pass

    def read(self, category="all", force=False, *, limit=None):
        if category not in CATEGORIES:
            raise ValueError("unsupported movie category")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("invalid preview limit")
        rows, states, work = [], [], []
        now = self.clock()
        for provider in self.providers:
            for kind in provider.categories:
                if category != "all" and category != kind:
                    continue
                key = provider.key + ":" + kind
                with self._lock:
                    cached = deepcopy(self._cache.get(key))
                if (not force and isinstance(cached, dict) and type(cached.get("at")) in (int, float)
                        and 0 <= now - cached["at"] < self.TTL and isinstance(cached.get("rows"), list)):
                    rows.extend(cached["rows"])
                    states.append(dict(platform=provider.name, kind=kind, state="cached", at=cached["at"]))
                else:
                    with self._lock:
                        generation = self._generations.get(key, 0) + 1
                        self._generations[key] = generation
                    work.append((key, provider, kind, cached, generation))
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="MoviePublicRank") as pool:
            futures = {pool.submit(provider.read, kind): (key, provider, kind, cached, generation)
                       for key, provider, kind, cached, generation in work}
            for future in as_completed(futures):
                key, provider, kind, cached, generation = futures[future]
                try:
                    found = future.result()
                    if not isinstance(found, list):
                        raise ValueError("invalid provider result")
                    at = self.clock()
                    rows.extend(found)
                    states.append(dict(platform=provider.name, kind=kind, state="success" if found else "empty", at=at))
                    with self._lock:
                        if self._generations.get(key) == generation:
                            self._cache[key] = {"at": at, "rows": deepcopy(found)}
                except Exception as exc:
                    if isinstance(exc, CatalogReadError):
                        code = exc.category
                    elif isinstance(exc, requests.Timeout):
                        code = "timeout"
                    elif isinstance(exc, requests.HTTPError):
                        code = "http"
                    elif isinstance(exc, requests.RequestException):
                        code = "network"
                    else:
                        code = "response"
                    logger.warning("movie_rank_failed provider=%s category=%s type=%s", provider.key, kind, type(exc).__name__)
                    fallback = cached if isinstance(cached, dict) and isinstance(cached.get("rows"), list) else None
                    if fallback:
                        rows.extend(fallback["rows"])
                    states.append(dict(platform=provider.name, kind=kind, state="stale" if fallback else "failed",
                                       at=fallback.get("at") if fallback else None, error=code))
        with self._lock:
            try:
                atomic_json(self.cache_path, {"version": 1, "boards": self._cache})
            except OSError:
                logger.warning("movie_rank_cache_save_failed")
        # Provider completion order must not shuffle cards between refreshes.
        rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get("title"), str)]
        rows.sort(key=lambda row: (str(row.get("kind")), row.get("rank") if type(row.get("rank")) is int else 1000000,
                                   str(row.get("platform")), str(row.get("source_id"))))
        works = merge_works(rows)
        totals = {kind: sum(row.get("kind") == kind for row in works) for kind in CATEGORIES if kind != "all"}
        if limit is not None:
            # The current public endpoint has no pagination. Cache its full
            # response, but hand only a small per-category preview to the UI.
            counts = {}
            preview = []
            for row in works:
                kind = row.get("kind")
                if counts.get(kind, 0) < limit:
                    preview.append(row)
                    counts[kind] = counts.get(kind, 0) + 1
            works = preview
        return {"works": works, "states": states, "totals": totals,
                "official_only": [p for p in PLATFORMS if not p.automatic]}
