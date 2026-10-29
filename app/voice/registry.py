"""Feature Registry: 统一功能登记与语义匹配入口。

ASR 文本 → 动作/概念/参数提取 → Registry 检索 → 结构化意图 → CommandBus。
只登记真实存在、有执行入口的功能。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


# 通用动作映射（集中维护，不在每个功能重复）
_ACTION_MAP: dict[str, str] = {
    "播放": "play", "开": "on", "开启": "on", "打开": "on", "开一下": "on",
    "关": "off", "关闭": "off", "关掉": "off",
    "下一首": "next", "下一曲": "next", "换一首": "next",
    "下一部": "next_film", "下一集": "next_episode",
    "暂停": "pause", "继续": "resume",
}


@dataclass
class Feature:
    feature_id: str
    domain: str            # music / movie / game / system
    name: str
    concepts: list[str]    # 概念关键词
    actions: list[str]     # 支持的动作
    executor: str          # CommandType 名
    params: dict[str, Any] = field(default_factory=dict)
    precondition: str = ""


# 已登记功能清单（真实存在执行入口）
REGISTRY: list[Feature] = [
    Feature(
        feature_id="music.highlight_mode",
        domain="music",
        name="高潮模式",
        concepts=["高潮模式", "高潮"],
        actions=["on", "off"],
        executor="MUSIC_HIGHLIGHT_MODE",
    ),
    Feature(
        feature_id="music.next",
        domain="music",
        name="下一首",
        concepts=["下一首", "下一曲"],
        actions=["next"],
        executor="MUSIC_NEXT",
    ),
    Feature(
        feature_id="movie.next_film",
        domain="movie",
        name="下一部电影",
        concepts=["下一部"],
        actions=["next_film"],
        executor="MOVIE_NEXT_EPISODE",
    ),
    Feature(
        feature_id="movie.next_episode",
        domain="movie",
        name="下一集",
        concepts=["下一集"],
        actions=["next_episode"],
        executor="MOVIE_NEXT_EPISODE",
    ),
]


def match(text: str) -> dict[str, Any] | None:
    """通用语义匹配：从 ASR 文本提取动作+概念，检索 Registry。
    返回结构化意图；未命中或歧义返回 None。"""
    text = text.strip()
    # 提取动作
    action = None
    for kw, act in _ACTION_MAP.items():
        if kw in text:
            action = act
            break
    if not action:
        return None
    # 提取概念并匹配功能
    candidates = []
    for f in REGISTRY:
        if action in f.actions:
            for c in f.concepts:
                if c in text:
                    candidates.append(f)
                    break
    if len(candidates) == 1:
        f = candidates[0]
        return {
            "domain": f.domain,
            "action": action,
            "feature_id": f.feature_id,
            "executor": f.executor,
            "params": {"enabled": action == "on"} if f.executor == "MUSIC_HIGHLIGHT_MODE" else {},
            "match_quality": "explicit",
        }
    # 歧义或未命中
    return None
