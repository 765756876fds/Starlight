"""
在线音源管理 - 参考洛雪音乐格式
支持添加自定义在线音源，JSON配置格式
"""
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from app.core.path_resolve import PROJECT_ROOT, USER_DATA_DIR


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
            config_dir = str(PROJECT_ROOT / config_dir)
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

    def import_from_json(self, file_path: str) -> MusicSource:
        """从JSON/JS文件导入音源配置"""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # 先尝试解析JSON
        try:
            data = json.loads(content)
            source = MusicSource.from_dict(data)
        except Exception:
            # 如果是JS文件，提取头部信息
            source = MusicSource()
            # 提取@name
            import re
            name_match = re.search(r'@name\s+(\S+)', content)
            if name_match:
                source.name = name_match.group(1)
            else:
                source.name = os.path.basename(file_path).replace(".js", "")
            
            # 提取API_URL
            api_match = re.search(r'API_URL\s*=\s*["\']([^"\']+)["\']', content)
            if api_match:
                source.api_url = api_match.group(1)
            
            source.source_type = "js"
        
        if not source.name:
            source.name = os.path.basename(file_path).replace(".json", "").replace(".js", "")
        self.add_source(source)
        return source

    def import_from_url(self, url: str) -> MusicSource:
        """从URL在线导入音源配置"""
        import requests
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        source = MusicSource.from_dict(data)
        if not source.name:
            source.name = "在线音源"
        self.add_source(source)
        return source

    def get_all_sources(self) -> list[MusicSource]:
        """获取所有音源"""
        return self.sources.copy()
