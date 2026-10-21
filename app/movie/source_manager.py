"""电影源管理：多源配置（settings.yaml movie.sources 持久化）+ 三合一测源。

设计（包2c后续，用户批准）：
- 内置 9 个实测可用源 DEFAULT_SOURCES（量子置顶）
- settings.yaml movie.sources 用户增删持久化：一旦存在即以 settings 为准
  （用户删除永久生效，不会从代码默认加回来）
- active_source：搜索当前源（策略 C：当前源挂了自动换，最多 3 个）
- test_source：三合一（L1 连通 HTTP200+JSON / L2 搜"流浪地球2"有结果 / L3 首条带 m3u8 且响应含 #EXTM3U）
"""
import json
import os
import threading
from typing import Any

import requests
import yaml

from app.core.path_resolve import PROJECT_ROOT

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 实测可用源（2026-10-16 探针：17 源里 9 个通过三合一）——量子置顶
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"name": "量子资源", "url": "http://cj.lziapi.com/api.php/provide/vod/from/lzm3u8/", "enabled": True},
    {"name": "非凡资源", "url": "http://cj.ffzyapi.com/api.php/provide/vod/from/ffm3u8/", "enabled": True},
    {"name": "暴风资源", "url": "https://bfzyapi.com/api.php/provide/vod/from/bfzym3u8/", "enabled": True},
    {"name": "光速资源", "url": "https://api.guangsuapi.com/api.php/provide/vod/from/gsm3u8/", "enabled": True},
    {"name": "极速资源", "url": "https://jszyapi.com/api.php/provide/vod/from/jsm3u8/", "enabled": True},
    {"name": "豪华资源", "url": "https://hhzyapi.com/api.php/provide/vod/from/hhm3u8/", "enabled": True},
    {"name": "无尽资源", "url": "https://api.wujinapi.com/api.php/provide/vod/from/wjm3u8/", "enabled": True},
    {"name": "360资源", "url": "https://360zy.com/api.php/provide/vod/", "enabled": True},
    {"name": "电影天堂", "url": "http://caiji.dyttzyapi.com/api.php/provide/vod/from/dyttm3u8/", "enabled": True},
]

_TEST_KW = "流浪地球2"


def _default_settings_path() -> str:
    return str(PROJECT_ROOT / "config" / "settings.yaml")


def _check_hls(url: str, timeout: float) -> bool:
    """GET m3u8 前 1KB，含 HLS 标记即视为可播。"""
    try:
        r = requests.get(url, timeout=timeout, headers=UA, stream=True,
                         allow_redirects=True)
        head = b""
        for chunk in r.iter_content(2048):
            head += chunk
            if len(head) >= 1024:
                break
        r.close()
        text = head.decode("utf-8", "ignore")
        return ("#EXTM3U" in text or "#EXT-X-STREAM-INF" in text
                or ".m3u8" in text)
    except Exception:
        return False


class MovieSourceManager:
    """电影源管理器：settings.yaml movie.sources 持久化 + 三合一测源。"""

    _lock = threading.Lock()

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path or _default_settings_path()
        self._sources: list[dict[str, Any]] = []
        self._active: str | None = None
        self._load()

    # ---- 加载/保存 ----

    def _load(self):
        data = self._read_yaml()
        m = data.get("movie") or {}
        stored = m.get("sources")
        if isinstance(stored, list) and stored:
            # 用户已接管（首次增删/启用/设当前后写回）：删除永久生效
            self._sources = [self._norm(s) for s in stored]
        else:
            self._sources = [dict(s) for s in DEFAULT_SOURCES]
        active = m.get("active_source")
        if not active and self._sources:
            active = self._sources[0]["name"]
        self._active = active

    def _read_yaml(self) -> dict:
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[MovieSource] 读取配置失败: {e}")
        return {}

    def _save(self):
        """写回 movie.sources + active_source（保留 settings.yaml 其他节）。

        防覆盖写（P1）：文件已存在但读取失败（损坏/权限/空文件——_read_yaml
        此时返回 {}）→ 中止不写回，绝不把残缺配置（仅 movie 节）覆盖到用户配置。
        """
        with self._lock:
            exists = os.path.exists(self._config_path)
            data = self._read_yaml()
            if exists and not data:
                print("[MovieSource] 现有配置读取失败，已中止保存（防覆盖写）")
                return
            m = data.setdefault("movie", {})
            if not isinstance(m, dict):
                m = {}
                data["movie"] = m
            m["sources"] = [dict(s) for s in self._sources]
            m["active_source"] = self._active or (self._sources[0]["name"] if self._sources else "")
            try:
                with open(self._config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            except Exception as e:
                print(f"[MovieSource] 保存配置失败: {e}")

    @staticmethod
    def _norm(s) -> dict[str, Any]:
        return {
            "name": str(s.get("name") or "").strip() or "未命名源",
            "url": str(s.get("url") or "").strip(),
            "enabled": bool(s.get("enabled", True)),
        }

    # ---- 查询 ----

    def get_all_sources(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._sources]

    def get_enabled_sources(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._sources if s.get("enabled", True)]

    def get_active_name(self) -> str | None:
        if self._active:
            return self._active
        srcs = self._sources
        return srcs[0]["name"] if srcs else None

    def get_active_source(self) -> dict[str, Any] | None:
        name = self.get_active_name()
        for s in self._sources:
            if s["name"] == name:
                return dict(s)
        return None

    # ---- 增删改 ----

    def add_source(self, name: str, url: str) -> bool:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url:
            return False
        for s in self._sources:
            if s["name"] == name:
                return False
        self._sources.append({"name": name, "url": url, "enabled": True})
        self._save()
        return True

    def remove_source(self, name: str):
        before = len(self._sources)
        self._sources = [s for s in self._sources if s["name"] != name]
        if len(self._sources) != before:
            if self._active == name:
                self._active = self._sources[0]["name"] if self._sources else None
            self._save()

    def enable_source(self, name: str, enabled: bool):
        for s in self._sources:
            if s["name"] == name:
                s["enabled"] = bool(enabled)
                break
        self._save()

    def set_active(self, name: str):
        if any(s["name"] == name for s in self._sources):
            self._active = name
            self._save()

    # ---- 测源（三合一） ----

    @staticmethod
    def test_source(src: dict[str, Any], timeout: float = 8.0):
        """三合一：L1 连通 / L2 搜索有结果 / L3 首条 m3u8 可播。返回 (ok, detail)。"""
        url = (src or {}).get("url", "")
        if not url:
            return False, "无 URL"
        try:
            r = requests.get(url, params={"ac": "videolist", "wd": _TEST_KW},
                             timeout=timeout, headers=UA)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            body = r.text.lstrip()
            if not body or body[0] not in "{[":
                return False, "非 JSON 响应"
            try:
                data = json.loads(body)
            except Exception:
                return False, "JSON 解析失败"
            lst = data.get("list") or []
            if not lst:
                return False, "搜索无结果"
            pu = str(lst[0].get("vod_play_url") or "")
            if "$" not in pu:
                return False, "play_url 无 m3u8"
            m3u8_url = pu.split("$", 1)[1].split("#")[0].strip()
            if not m3u8_url:
                return False, "play_url 无 m3u8"
            if not _check_hls(m3u8_url, timeout=timeout):
                return False, "m3u8 响应异常"
            return True, "可用"
        except requests.exceptions.Timeout:
            return False, "超时"
        except Exception as e:
            return False, type(e).__name__

    def test_all(self, sources: list[dict[str, Any]] | None = None,
                 timeout: float = 8.0):
        """批量测试。返回 [(name, ok, detail), ...]。"""
        srcs = sources if sources is not None else self._sources
        out = []
        for s in srcs:
            ok, detail = self.test_source(s, timeout=timeout)
            out.append((s.get("name") or "", ok, detail))
        return out


class _SingleSourceManager:
    """测试/兼容：显式 source_url → 单一源（旧行为，search 零分支差异）。"""

    def __init__(self, url: str):
        self._src = {"name": "单源", "url": url, "enabled": True}

    def get_all_sources(self):
        return [dict(self._src)]

    def get_enabled_sources(self):
        return [dict(self._src)]

    def get_active_name(self) -> str | None:
        return "单源"
