"""
OnlineResolver —— 在线歌曲纯网络解析器

职责边界：接收 query（歌名+歌手）或 track 对象，返回真实的播放 url 及
track 元数据。内部完成：多源搜索 → 候选排序（音质/耗时/酷我兜底）→
逐个源试 get_play_url（failover）。

不碰 mpv、不碰播放队列、不碰 Qt 信号——解析结果由调用方
（MusicController 的后台 worker）消费并决定是否落盘播放。
"""
import re
from typing import Any

from ...core.logger import get_logger

logger = get_logger()


# mpv http-header-fields 用逗号分隔，值含逗号会破坏语法；只转发安全字段。
_HEADER_WHITELIST = ("Referer", "User-Agent", "Cookie", "Authorization")
_WL_LOWER = {h.lower() for h in _HEADER_WHITELIST}


def _pick_play_headers(headers):
    """过滤插件 headers：只留白名单字段（大小写不敏感，HTTP 头大小写不敏感），
    值含逗号跳过；空返回 None。"""
    if not headers or not isinstance(headers, dict):
        return None
    out = {}
    for k, v in headers.items():
        if k.lower() not in _WL_LOWER:
            continue
        if not isinstance(v, str) or "," in v:
            continue
        out[k] = v
    return out or None


# ---------- 在线选源：同一首歌多源时，音质最优 → 同音质选最快 ----------

def _quality_score(source_name: str, raw) -> int:
    """从插件原始 item 提取音质等级：4=HiRes 3=高品/无损 2=标准 1=低 0=未知。
    字段名来自真实搜索结果核对（kg/kw/wy 用 qualities 档位；溯音用 URL 码率）。"""
    if not isinstance(raw, dict):
        return 0
    src = (source_name or "").lower()
    # 溯音网易：search 直接给 url（share.duanx.cn/.../128k）→ 按 URL 实际码率
    if "溯音" in src:
        m = re.search(r"/(\d+)k", str(raw.get("url") or ""))
        if m:
            b = int(m.group(1))
            return 3 if b >= 320 else (2 if b >= 192 else 1)
        return 0
    # 通用 qualities 档位（kg/kw/wy 真实结构）
    q = raw.get("qualities")
    if isinstance(q, dict):
        keys = set(q.keys())
        if keys & {"super", "lossless", "hires"}: return 4
        if "high" in keys: return 3
        if "standard" in keys: return 2
        if "low" in keys: return 1
        return 0
    # tx 腾讯（当前搜索返回 0 条，留字段兼容）
    if "tx" in src or "腾讯" in src:
        if raw.get("size_flac"): return 3
        if raw.get("size_320mp3"): return 2
        return 0
    return 0


def _normalize(s: str) -> str:
    """归一化：小写 + 去标点空格 + 去 feat. 后缀。归一化相同 = 同一首歌。"""
    s = (s or "").strip().lower()
    s = re.sub(r"[\[\(]feat\..*?[\]\)]", "", s)   # (feat. xx) / [feat. xx]
    s = re.sub(r"\W+", "", s)                      # 去标点/空格/符号
    return s


def _kw_bonus(name: str) -> int:
    """酷我系优先（验证过能播）——同音质同耗时的兜底 tie-break。"""
    n = (name or "").lower()
    return 0 if ("kw" in n or "酷我" in n) else 1


def _pick_candidates(all_tracks, manager):
    """首选组（同一首歌）内按 (音质降序, 耗时升序, 酷我优先, 原顺序) 排序的
    候选列表——调用方逐个试 get_play_url，直到拿到 URL（failover）。
    组间取"首个 source 第一条"所在组（保留原"搜到啥播啥"的相关性语义，
    不全局抢音质）。"""
    if not all_tracks:
        return []
    groups = {}
    for idx, r in enumerate(all_tracks):
        key = _normalize(r.title) + "|" + _normalize(r.artist)
        groups.setdefault(key, []).append((idx, r))
    # 首选组 = 首个 source 第一条所在组（原行为 all_tracks[0] 的歌，相关性最高）
    first_key = _normalize(all_tracks[0].title) + "|" + _normalize(all_tracks[0].artist)
    group = groups[first_key]

    def _lat(name):
        v = manager.get_latency(name) if hasattr(manager, "get_latency") else None
        return v if isinstance(v, (int, float)) else 99.0
    group.sort(key=lambda t: (-_quality_score(t[1].source_name, t[1].extra.get("_raw")),
                              _lat(t[1].source_name),
                              _kw_bonus(t[1].source_name),
                              t[0]))
    return [t[1] for t in group]


def _pick_best(all_tracks, manager):
    """首选（兼容旧接口）：候选列表第一个，无候选返回 None。"""
    cands = _pick_candidates(all_tracks, manager)
    return cands[0] if cands else None


class OnlineResolver:
    """纯网络解析器：把 query/track 变成可播放的 url + 元数据。

    不碰 mpv、不碰播放队列、不碰 Qt 信号。解析结果由调用方
    （Controller 的后台 worker）消费；调用方负责 request_id 过期检查与
    loadfile 播放。
    """

    def __init__(self, manager=None):
        self._manager = manager

    def set_manager(self, manager):
        """注入在线源管理器（可延迟到源就绪后）。"""
        self._manager = manager

    def search_tracks(self, query: str, limit_per_source: int = 5) -> list:
        """多源搜索并合并 track 列表（保持源遍历顺序）。"""
        if self._manager is None:
            return []
        online_dict = self._manager.search_all(query, limit_per_source=limit_per_source)
        return [t for tracks in online_dict.values() for t in tracks]

    def pick_candidates(self, all_tracks: list) -> list:
        """候选排序（音质降序 → 耗时升序 → 酷我兜底），调用方逐个试 URL。"""
        return _pick_candidates(all_tracks, self._manager)

    def resolve(self, query: str, limit_per_source: int = 5):
        """完整管线：搜索 → 候选排序 → 逐个源试 get_play_url（failover）。

        返回 {'url', 'track', 'play_result'}（首个拿到 URL 的候选），
        无候选或全部失败返回 None。
        """
        all_tracks = self.search_tracks(query, limit_per_source)
        if not all_tracks:
            logger.info("[Resolver] 搜索无结果: %s", query)
            return None
        for cand in self.pick_candidates(all_tracks):
            play_result = self._manager.get_play_url(cand)
            if play_result and play_result.get("url"):
                return {
                    "url": play_result["url"],
                    "track": cand,
                    "play_result": play_result,
                }
        logger.warning("[Resolver] 全部候选均无播放 URL: %s", query)
        return None
