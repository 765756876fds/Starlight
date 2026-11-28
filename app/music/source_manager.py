"""
在线音源管理 - 参考洛雪音乐格式
支持添加自定义在线音源，JSON配置格式
"""
import json
import os
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from app.core.path_resolve import USER_DATA_DIR, resolve_user_path


@dataclass
class MusicSource:
    """单个音乐源配置"""
    name: str = ""
    api_url: str = ""
    search_path: str = "/search"
    song_url_path: str = "/song/url"
    enabled: bool = True
    # 元信息
    source_type: str = "online"  # online / local
    added_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MusicSource":
        return cls(
            name=data.get("name", "未命名音源"),
            api_url=data.get("api_url", ""),
            search_path=data.get("search_path", "/search"),
            song_url_path=data.get("song_url_path", "/song/url"),
            enabled=data.get("enabled", True),
            source_type=data.get("source_type", "online"),
            added_time=data.get("added_time", 0.0),
        )


class MusicSourceManager:
    """音乐源管理器"""

    def __init__(self, config_dir: str = ""):
        # R4（2026-10-27）：默认落到 USER_DATA_DIR/config —— 源码态 == PROJECT_ROOT/config
        # （行为不变）；打包态（frozen）写到 %LOCALAPPDATA%\Starlight\config（Program Files
        # 只读，自定义音源启用/导入写不进 exe 目录），与 settings.yaml 读取路径一致。
        if not config_dir:
            config_dir = str(USER_DATA_DIR / "config")
        elif not os.path.isabs(config_dir):
            config_dir = resolve_user_path(config_dir)
        self.config_dir = config_dir
        self.sources_file = os.path.join(config_dir, "music_sources.json")
        self.sources: list[MusicSource] = []
        self._load()

    def _load(self):
        """从文件加载音源配置"""
        os.makedirs(self.config_dir, exist_ok=True)
        if os.path.exists(self.sources_file):
            try:
                with open(self.sources_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.sources = [MusicSource.from_dict(s) for s in data.get("sources", [])]
            except Exception as e:
                print(f"[音源] 加载配置失败: {e}")
                self.sources = []

    def _save(self):
        """保存音源配置到文件"""
        os.makedirs(self.config_dir, exist_ok=True)
        data = {
            "sources": [s.to_dict() for s in self.sources]
        }
        with open(self.sources_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_source(self, source: MusicSource) -> bool:
        """添加新音源"""
        # 检查重名
        for s in self.sources:
            if s.name == source.name:
                return False
        self.sources.append(source)
        self._save()
        return True

    def remove_source(self, name: str):
        """删除音源"""
        self.sources = [s for s in self.sources if s.name != name]
        self._save()

    def enable_source(self, name: str, enabled: bool):
        """启用/禁用音源"""
        for s in self.sources:
            if s.name == name:
                s.enabled = enabled
                break
        self._save()

    def get_enabled_sources(self) -> list[MusicSource]:
        """获取所有启用的音源"""
        return [s for s in self.sources if s.enabled]

    @staticmethod
    def _configuration(data: Any) -> MusicSource:
        if not isinstance(data, dict) or data.get("source_type", "online") != "online":
            raise ValueError("Expected an HTTP JSON source configuration")
        name = data.get("name")
        url = data.get("api_url")
        if not isinstance(name, str) or not name.strip() or not isinstance(url, str):
            raise ValueError("Missing source name or API URL")
        target = urlsplit(url)
        if target.scheme not in ("http", "https") or not target.hostname or target.username is not None:
            raise ValueError("Expected an HTTP(S) API URL without embedded credentials")
        for field in ("search_path", "song_url_path"):
            if field in data and not isinstance(data[field], str):
                raise ValueError("Invalid endpoint")
        if not isinstance(data.get("enabled", True), bool):
            raise ValueError("Invalid enabled state")
        return MusicSource.from_dict({**data, "name": name.strip()})

    def import_from_json(self, file_path: str) -> MusicSource:
        """导入声明式配置。无效文件或重名不会更改现有音源。"""
        with open(file_path, "rb") as stream:
            payload = stream.read(65537)
        return self.import_configuration(payload)

    @staticmethod
    def fetch_configuration(url: str) -> bytes:
        import requests

        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("Expected an HTTP(S) configuration URL")
        with requests.get(url, timeout=(3, 5), stream=True) as response:
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                payload.extend(chunk)
                if len(payload) > 65536:
                    raise ValueError("Configuration is too large")
        return bytes(payload)

    def import_configuration(self, payload: bytes) -> MusicSource:
        if len(payload) > 65536:
            raise ValueError("Configuration is too large")
        source = self._configuration(json.loads(payload.decode("utf-8-sig")))
        if not self.add_source(source):
            raise ValueError("已存在同名音源，请使用其他名称")
        return source

    def get_all_sources(self) -> list[MusicSource]:
        """获取所有音源"""
        return self.sources.copy()
