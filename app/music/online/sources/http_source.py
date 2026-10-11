"""通用 HTTP JSON 音源：把「音源管理」UI 导入的自定义音源（api_url + search_path +
song_url_path）变成可参与搜索/播放的 MusicSource。

响应结构参考洛雪音乐风格，宽松解析多种常见字段；请求失败会抛出带分类的
SourceError（由 OnlineSourceManager 记录到 _errors，不影响其他音源），
真正的"无搜索结果"返回空列表。日志不打印完整 URL / 参数 / Token。
"""
from __future__ import annotations

from typing import List, Optional

import requests

from ..models import OnlineTrack
from ..source import MusicSource


class SourceError(Exception):
    """音源请求失败，带分类：network / network_timeout / http_<code> /
    json / structure / config。"""

    def __init__(self, category: str, source_name: str = "", detail: str = ""):
        self.category = category
        self.source_name = source_name
        self.detail = detail
        super().__init__(f"source={source_name} category={category}")


_ERR_LABEL = {
    "network": "网络异常",
    "network_timeout": "网络超时",
    "json": "JSON 解析错误",
    "structure": "响应结构不兼容",
    "config": "配置缺失",
}


def _err_label(category: str) -> str:
    if category.startswith("http_"):
        return f"HTTP 错误(状态码 {category[5:]})"
    return _ERR_LABEL.get(category, category)


class HttpJsonSource(MusicSource):
    name: str = "http"
    display_name: str = "HTTP JSON 音源"

    def __init__(self, name: str, api_url: str, search_path: str = "/search",
                 song_url_path: str = "/song/url", timeout: float = 3.0):
        self.name = name or "http"
        self.display_name = name or "HTTP JSON 音源"
        self._api_url = (api_url or "").rstrip("/")
        self._search_path = search_path or "/search"
        self._song_url_path = song_url_path or "/song/url"
        self._timeout = timeout
        # 慢源兜底：search_all 对单个 future 的等待上限（源自身先超时，正常 ≤3s）
        self.search_timeout = timeout

    # ---- 内部工具 ----

    def _join(self, path: str) -> str:
        """URL 路径拼接：path 已是完整 URL 时直接使用，否则拼在 api_url 之后。"""
        if not path:
            return self._api_url
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._api_url}{path}"

    def _get_json(self, path: str, params: dict):
        """返回 (data, err)。err 为 None 表示成功；否则为错误分类字符串。"""
        if not self._api_url:
            return None, "config"
        try:
            resp = requests.get(
                self._join(path),
                params=params,
                timeout=self._timeout,
            )
        except requests.Timeout:
            return None, "network_timeout"
        except requests.RequestException:
            return None, "network"
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", 0)
            return None, f"http_{code}"
        try:
            data = resp.json()
        except ValueError:
            return None, "json"
        return data, None

    @staticmethod
    def _extract_artist(value) -> str:
        """artist/singer 可能是字符串、字典（{name:...}）或列表。"""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            v = value.get("name") or value.get("artist") or ""
            return str(v).strip()
        if isinstance(value, list):
            parts = []
            for x in value:
                if isinstance(x, str):
                    parts.append(x.strip())
                elif isinstance(x, dict):
                    parts.append(str(x.get("name") or "").strip())
            return "、".join(p for p in parts if p)
        return ""

    @staticmethod
    def _extract_track(item: dict, source_name: str) -> Optional[OnlineTrack]:
        """宽松提取一首歌：兼容 name/title/songName、artist/singer、
        id/songId/songmid 等常见字段。album 可能为字符串或字典。"""
        if not isinstance(item, dict):
            return None
        title = (item.get("name") or item.get("title")
                 or item.get("songName") or "").strip()
        if not title:
            return None
        artist = HttpJsonSource._extract_artist(
            item.get("artist") or item.get("singer") or item.get("author")
        )
        track_id = str(item.get("id") or item.get("songId")
                       or item.get("songmid") or item.get("rid") or "")
        if not track_id:
            return None
        raw_album = item.get("album") or item.get("al")
        if isinstance(raw_album, dict):
            album = str(raw_album.get("name") or "").strip()
        elif isinstance(raw_album, str):
            album = raw_album.strip()
        else:
            album = ""
        try:
            duration = int(item.get("duration") or item.get("interval") or 0)
        except (TypeError, ValueError):
            duration = 0
        return OnlineTrack(
            source_name=source_name,
            track_id=track_id,
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration,
            extra={"raw": item},
        )

    # ---- MusicSource 接口 ----

    def search(self, keyword: str, limit: int = 20) -> List[OnlineTrack]:
        if not keyword or not self._api_url:
            return []
        data, err = self._get_json(self._search_path, {
            "keyword": keyword,
            "limit": int(limit),
        })
        if err:
            # 源失败静默执行：错误仍分类记录到 manager._errors，但不再打印到控制台
            raise SourceError(err, self.name)
        if data is None:
            raise SourceError("structure", self.name)
        # 注意不能用 or 链：空列表是合法"无结果"，会被 or 当成 falsy 吞掉
        items = data.get("data")
        if items is None:
            items = data.get("result")
        if items is None:
            items = data.get("songs")
        if isinstance(data, list):
            items = data
        if not isinstance(items, list):
            raise SourceError("structure", self.name)
        out: List[OnlineTrack] = []
        for item in items:
            t = self._extract_track(item, self.name)
            if t:
                out.append(t)
                if len(out) >= limit:
                    break
        if items and not out:
            # 有数据但没有任何一条可解析 → 结构不兼容
            raise SourceError("structure", self.name)
        return out

    def get_play_url(self, track: OnlineTrack) -> Optional[str]:
        if not self._api_url:
            return None
        data, err = self._get_json(self._song_url_path, {"id": track.track_id})
        if err:
            # 源失败静默执行：错误仍分类记录到 manager._errors，但不再打印到控制台
            raise SourceError(err, self.name)
        if not isinstance(data, dict):
            raise SourceError("structure", self.name)
        url = data.get("url") or (data.get("data") or {}).get("url")
        if isinstance(data.get("data"), str):
            url = data["data"]
        if not isinstance(url, str) or not url:
            return None
        return url

    def get_lyric(self, track: OnlineTrack) -> Optional[str]:
        return None
