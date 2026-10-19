# -*- coding: utf-8 -*-
"""电影榜单多平台 Provider 架构（批：多平台榜单）。

BaseRankProvider 定义榜单源统一契约；DoubanProvider 封装现有豆瓣逻辑（无损）。
降级契约继承自 douban_rank：任何 Provider 的失败都必须返回 []，绝不抛——
UI 侧统一提示"榜单加载失败"，不崩。
"""
from . import douban_rank


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


def default_provider():
    """默认榜单源（豆瓣）。UI 初始加载用它，体验不变。"""
    return DoubanProvider()
