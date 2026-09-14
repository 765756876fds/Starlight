"""
Live Client Data API (2999) 客户端

读取游戏内实时数据：
- /allgamedata
- /activeplayer
- /activeplayerabilities
- /activeplayerrunes
- /playerlist
- /playerscores
- /playersummonerspells
- /playeritems
- /gamestats
- /eventdata

这是游戏内核心数据层，所有数据从这里来。
"""
import json
import threading
import time
from typing import Any, Dict, List, Optional

import requests
import urllib3

# 禁用 SSL 警告（本地自签名证书）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LiveClientAPI:
    """
    Live Client Data API 客户端

    游戏运行时在 https://127.0.0.1:2999 提供数据。
    """

    ENDPOINTS = {
        "allgamedata": "/allgamedata",
        "activeplayer": "/activeplayer",
        "activeplayerabilities": "/activeplayerabilities",
        "activeplayerrunes": "/activeplayerrunes",
        "playerlist": "/playerlist",
        "playerscores": "/playerscores",
        "playersummonerspells": "/playersummonerspells",
        "playeritems": "/playeritems",
        "gamestats": "/gamestats",
        "eventdata": "/eventdata",
    }

    def __init__(self, base_url: str = "https://127.0.0.1:2999",
                 verify_ssl: bool = False, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = requests.Session()
        self._session.verify = verify_ssl
        self._lock = threading.Lock()
        self._available = False
        self._last_check = 0.0

    def is_available(self) -> bool:
        """检查游戏 API 是否可用"""
        now = time.time()
        if now - self._last_check < 1.0:
            return self._available

        self._last_check = now
        try:
            resp = self._session.get(
                f"{self.base_url}/gamestats",
                timeout=self.timeout
            )
            self._available = resp.status_code == 200
        except (requests.RequestException, OSError):
            self._available = False

        return self._available

    def _get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """发送 GET 请求"""
        url = f"{self.base_url}{endpoint}"
        try:
            with self._lock:
                resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            return None
        except (requests.RequestException, json.JSONDecodeError, OSError):
            return None

    def get_all_game_data(self) -> Optional[Dict[str, Any]]:
        """获取完整游戏数据（用于测试和探索）"""
        return self._get(self.ENDPOINTS["allgamedata"])

    def get_active_player(self) -> Optional[Dict[str, Any]]:
        """获取当前玩家信息"""
        return self._get(self.ENDPOINTS["activeplayer"])

    def get_active_player_abilities(self) -> Optional[Dict[str, Any]]:
        """获取当前玩家技能"""
        return self._get(self.ENDPOINTS["activeplayerabilities"])

    def get_active_player_runes(self) -> Optional[Dict[str, Any]]:
        """获取当前玩家符文"""
        return self._get(self.ENDPOINTS["activeplayerrunes"])

    def get_player_list(self) -> Optional[List[Dict[str, Any]]]:
        """获取所有玩家列表"""
        return self._get(self.ENDPOINTS["playerlist"])

    def get_player_scores(self) -> Optional[List[Dict[str, Any]]]:
        """获取所有玩家分数"""
        return self._get(self.ENDPOINTS["playerscores"])

    def get_player_summoner_spells(self) -> Optional[List[Dict[str, Any]]]:
        """获取所有玩家召唤师技能"""
        return self._get(self.ENDPOINTS["playersummonerspells"])

    def get_player_items(self) -> Optional[List[Dict[str, Any]]]:
        """获取所有玩家物品"""
        return self._get(self.ENDPOINTS["playeritems"])

    def get_game_stats(self) -> Optional[Dict[str, Any]]:
        """获取游戏统计（包含游戏时间）"""
        return self._get(self.ENDPOINTS["gamestats"])

    def get_event_data(self) -> Optional[Dict[str, Any]]:
        """获取事件数据"""
        return self._get(self.ENDPOINTS["eventdata"])

    def get_game_time(self) -> float:
        """获取当前游戏时间（秒）"""
        stats = self.get_game_stats()
        if stats:
            return float(stats.get("gameTime", 0))
        return 0.0

    def close(self):
        """关闭会话"""
        self._session.close()
