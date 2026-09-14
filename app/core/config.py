"""
全局配置加载器
"""
import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


class Config:
    """单例配置管理器"""

    _instance: Optional["Config"] = None
    _data: Dict[str, Any] = {}

    def __new__(cls, config_path: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load(config_path)
        return cls._instance

    def _load(self, config_path: Optional[str] = None):
        """加载配置文件"""
        if config_path is None:
            # 默认路径: 项目根目录/config/settings.yaml
            base_dir = Path(__file__).parent.parent.parent
            config_path = base_dir / "config" / "settings.yaml"

        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f)

        # 确保数据目录存在
        data_dir = self.get("app.data_dir", "./data")
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(self.get("app.log_dir", "./logs"), exist_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值，支持点号分隔的嵌套键
        例如: config.get("music.mpv_path")
        """
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def get_section(self, section: str) -> Dict[str, Any]:
        """获取整个配置段"""
        return self._data.get(section, {})

    @property
    def data(self) -> Dict[str, Any]:
        return self._data.copy()


def get_config(config_path: Optional[str] = None) -> Config:
    """获取全局配置单例"""
    return Config(config_path)
