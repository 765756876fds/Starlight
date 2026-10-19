# -*- coding: utf-8 -*-
"""电影榜单多平台 Provider 架构（批：多平台榜单）。

BaseRankProvider 定义榜单源统一契约；DoubanProvider 封装现有豆瓣逻辑（无损）；
TgmengProvider 接入糖果梦开放平台热榜 API（热点检索式榜单）。
降级契约：任何 Provider 的失败都必须返回 []，绝不抛——UI 侧统一提示"榜单加载失败"。
"""
import hashlib
import json
import os
from pathlib import Path
import urllib.request

from . import douban_rank

# 糖果梦密钥本地配置（*.local.yaml 已被 .gitignore 忽略，不入库）
_TGMENG_CONFIG = Path("config/tgmeng.local.yaml")


class BaseRankProvider:
    """榜单源基类。所有子类必须实现 name/get_tags/get_subjects。"""

    #: 平台展示名（UI 下拉框/左侧标签 tooltip 使用）
    name = "Base"

    def get_tags(self) -> list:
        """榜单分类/标签列表（如豆瓣的 热门/最新/豆瓣高分…）。失败 → []。"""
        raise NotImplementedError

    def get_subjects(self, tag, page_start=0, page_limit=20) -> list:
        """按标签拉榜单条目，统一 schema：
        [{title: str, cover: str, rate: str, url: str, id: str}, ...]。失败 → []。
        """
        raise NotImplementedError


class DoubanProvider(BaseRankProvider):
    """豆瓣榜单（原 douban_rank 函数封装，行为与降级契约完全一致）。"""

    name = "豆瓣"

    def get_tags(self) -> list:
        return douban_rank.get_tags()

    def get_subjects(self, tag, page_start=0, page_limit=20) -> list:
        return douban_rank.get_subjects(tag, page_start=page_start,
                                        page_limit=page_limit)


class TgmengProvider(BaseRankProvider):
    """糖果梦开放平台热榜（热点检索式榜单）。

    端点：POST https://trendapi.tgmeng.com/api/skill/search
    鉴权：请求体 license 字段（不是 Header）。
    模式：REALTIME（实时热点）+ rootCategories=影音 过滤 + 关键词=电影类标签。
    注意：该 API 返回 items 无封面字段 → cover 留空，UI 走渐变兜底。
    降级契约：Key 缺失/失效/额度用完/超时 → []，绝不抛。
    """

    name = "糖果梦热榜"
    ENDPOINT = "https://trendapi.tgmeng.com/api/skill/search"
    ROOT_CATEGORY = "影音"
    # 电影类检索标签（每个标签作为关键词做 REALTIME 查询）
    TAGS = ["电影", "热映", "新片", "高分电影", "院线"]
    TIMEOUT = 8.0

    def __init__(self):
        self._license = self._load_license()

    @staticmethod
    def _load_license() -> str:
        """读取密钥：环境变量 TGMENG_LICENSE 优先，其次 config/tgmeng.local.yaml。"""
        env = os.environ.get("TGMENG_LICENSE", "").strip()
        if env:
            return env
        try:
            if _TGMENG_CONFIG.exists():
                import yaml
                cfg = yaml.safe_load(_TGMENG_CONFIG.read_text(encoding="utf-8")) or {}
                return str((cfg.get("tgmeng") or {}).get("license") or "").strip()
        except Exception:
            pass
        return ""

    def get_tags(self) -> list:
        if not self._license:
            return []
        return list(self.TAGS)

    def get_subjects(self, tag, page_start=0, page_limit=20) -> list:
        if not self._license:
            return []
        try:
            body = {
                "license": self._license,
                "keywords": [str(tag)],
                "mode": "REALTIME",
                "rootCategories": self.ROOT_CATEGORY,
                "limit": max(1, min(int(page_limit), 100)),
                "offset": max(0, int(page_start)),
                "distinct": True,
            }
            req = urllib.request.Request(
                self.ENDPOINT,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": "LoLMusicAgent/0.1"},
                method="POST")
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
                payload = json.loads(r.read().decode("utf-8", "ignore"))
            # 业务错误也可能以 HTTP 200 返回，必须检查响应体 code
            if payload.get("code") != 200:
                return []
            items = (payload.get("data") or {}).get("items") or []
            out = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("title") or "")
                url = str(it.get("url") or "")
                if not title:
                    continue
                out.append({
                    "title": title,
                    "cover": "",  # 糖果梦热点 API 无封面字段 → UI 渐变兜底
                    "rate": f"热度{it.get('rank')}" if it.get("rank") is not None else "",
                    "url": url,
                    "id": hashlib.md5((url or title).encode("utf-8")).hexdigest()[:12],
                })
            return out
        except Exception:
            return []


def default_provider():
    """默认榜单源（豆瓣）。UI 初始加载用它，体验不变。"""
    return DoubanProvider()
