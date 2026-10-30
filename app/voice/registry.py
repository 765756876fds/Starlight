"""Feature Registry: 统一功能登记与语义匹配入口。

ASR 文本 → 动作/概念/参数提取 → Registry 检索 → 结构化意图 → CommandBus。
只登记真实存在、有执行入口的功能。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import re
from ..core.command_bus import CommandType
from ..core.operation_result import OperationResult, OperationStatus


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    prompt: str
    example: str


# Shared by parameter clarification, execution validation and help tests.
PARAMETERS = {
    CommandType.MUSIC_SET_SPEED: ParameterSpec("speed", "请说出要设置的倍速，例如一点五倍", "倍速一点五倍"),
    CommandType.MUSIC_SLEEP_TIMER_SET: ParameterSpec("seconds", "请说出睡眠定时的时长，例如三十分钟", "睡眠定时三十分钟"),
    CommandType.MUSIC_VOLUME_SET: ParameterSpec("volume", "请说音量数值，范围零到一百", "音乐音量五十"),
    CommandType.MOVIE_VOLUME_SET: ParameterSpec("volume", "请说电影音量数值，范围零到一百", "电影音量五十"),
}


def parameter_issue(command, params):
    if command in (CommandType.AUTO_QUEUE_SET, CommandType.AUTO_ACCEPT_SET,
                   CommandType.AUTO_BANPICK_SET, CommandType.AUTO_COMBO_SET):
        if type(params.get("enabled")) is not bool:
            return OperationResult(OperationStatus.NEEDS_PARAMETER, "请明确说打开或关闭哪项自动化功能")
    spec = PARAMETERS.get(command)
    if spec and (spec.key not in params or params[spec.key] is None):
        return OperationResult(OperationStatus.NEEDS_PARAMETER, spec.prompt)
    if command in (CommandType.MUSIC_VOLUME_SET, CommandType.MOVIE_VOLUME_SET):
        value = params.get("volume")
        if type(value) not in (int, float) or not 0 <= value <= 100:
            return OperationResult(OperationStatus.UNSUPPORTED, "音量范围是零到一百")
    if command in (CommandType.MUSIC_SEEK, CommandType.MOVIE_SEEK):
        value = params.get("seconds")
        if type(value) not in (int, float) or not 1 <= abs(value) <= 60:
            return OperationResult(OperationStatus.NEEDS_PARAMETER, "请说快进或后退一到六十秒")
    if command in (CommandType.MUSIC_VOLUME_UP, CommandType.MUSIC_VOLUME_DOWN,
                   CommandType.MOVIE_VOLUME_UP, CommandType.MOVIE_VOLUME_DOWN) and "step" in params:
        if type(params["step"]) is not int or not 1 <= params["step"] <= 100:
            return OperationResult(OperationStatus.UNSUPPORTED, "请说一到一百之间的音量调整值")
    return None


def match_volume(text):
    """Anchored action/object aliases; no arbitrary substring inference."""
    if text in ("静音", "音乐静音"):
        return CommandType.MUSIC_VOLUME_SET, {"volume": 0}
    if text == "电影静音":
        return CommandType.MOVIE_VOLUME_SET, {"volume": 0}
    if re.fullmatch(r"(?:音乐)?音量", text):
        return CommandType.MUSIC_VOLUME_SET, {}
    if text == "电影音量":
        return CommandType.MOVIE_VOLUME_SET, {}
    m = re.fullmatch(r"(?:把)?(音乐|电影)(?:的)?(?:声音|音量)?(?:调)?(小|大)(?:一)?(?:点|点儿)", text)
    if not m:
        m = re.fullmatch(r"(音乐|电影)(小声|大声)(?:一)?点", text)
    if m:
        domain, direction = m.groups()
        command = "movie" if domain == "电影" else "music"
        return f"{command}.volume_{'down' if direction in ('小', '小声') else 'up'}", {}
    m = re.fullmatch(r"音量(?:调)?(降低|减小|提高|增大)(?:一)?点", text)
    if m:
        return (CommandType.MUSIC_VOLUME_DOWN if m.group(1) in ("降低", "减小")
                else CommandType.MUSIC_VOLUME_UP), {}
    return None


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
    """Compatibility view of the real parser; no second substring parser."""
    from .intent import RuleBasedIntent
    from .text import normalize_text
    intent = RuleBasedIntent.match(text)
    if intent is None:
        return None
    candidates = [f for f in REGISTRY if getattr(CommandType, f.executor) == intent.intent_type]
    if len(candidates) > 1:
        normalized = normalize_text(text)
        candidates = [f for f in candidates if any(c in normalized for c in f.concepts)]
    if len(candidates) == 1:
        f = candidates[0]
        action = ("on" if intent.params.get("enabled") else "off") if "enabled" in intent.params else f.actions[0]
        return {
            "domain": f.domain,
            "action": action,
            "feature_id": f.feature_id,
            "executor": f.executor,
            "params": dict(intent.params),
            "match_quality": "explicit",
        }
    # 歧义或未命中
    return None
