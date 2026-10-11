"""
洛雪音乐(LX Music) 开放API客户端
用于通过HTTP控制本地运行的落雪音乐播放器
"""
import requests
from typing import Any, Dict, List, Optional


class LxMusicClient:
    """洛雪音乐开放API客户端"""

    def __init__(self, host: str = "127.0.0.1", port: int = 23330):
        self.base_url = f"http://{host}:{port}"
        self.timeout = 5

    def _get(self, path: str) -> Dict[str, Any]:
        """GET请求"""
        try:
            resp = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[落雪API] GET {path} 失败: {e}")
            return {}

    def _post(self, path: str, data: Dict = None) -> Dict[str, Any]:
        """POST请求"""
        try:
            resp = requests.post(f"{self.base_url}{path}", json=data or {}, timeout=self.timeout)
            resp.raise_for_status()
            # 尝试解析JSON，失败就返回文本
            try:
                return resp.json()
            except:
                return {"raw": resp.text}
        except Exception as e:
            print(f"[落雪API] POST {path} 失败: {e}")
            return {}

    def is_connected(self) -> bool:
        """检查落雪音乐是否在线"""
        try:
            resp = requests.get(f"{self.base_url}/api/player/status", timeout=2)
            return resp.status_code == 200
        except:
            return False

    # ============ 播放控制 ============
    def toggle_play(self) -> bool:
        """切换播放/暂停"""
        return bool(self._post("/api/player/toggle"))

    def play(self) -> bool:
        """播放"""
        return bool(self._post("/api/player/play"))

    def pause(self) -> bool:
        """暂停"""
        return bool(self._post("/api/player/pause"))

    def next(self) -> bool:
        """下一首"""
        return bool(self._post("/api/player/skip-next"))

    def prev(self) -> bool:
        """上一首"""
        return bool(self._post("/api/player/skip-previous"))

    def set_volume(self, volume: int) -> bool:
        """设置音量 0-100"""
        volume = max(0, min(100, volume))
        return bool(self._post("/api/player/volume", {"volume": volume}))

    # ============ 状态查询 ============
    def get_status(self) -> Dict[str, Any]:
        """获取播放状态"""
        return self._get("/api/player/status")

    def get_play_list(self) -> List[Dict[str, Any]]:
        """获取播放列表"""
        data = self._get("/api/playlist")
        return data.get("list", [])

    # ============ 搜索播放 ============
    def search(self, keyword: str, source: str = "kw") -> List[Dict[str, Any]]:
        """搜索音乐"""
        data = self._post("/api/search", {
            "keyword": keyword,
            "source": source,
            "limit": 20
        })
        return data.get("list", [])

    def play_song(self, song_id: str, source: str = "kw") -> bool:
        """播放指定歌曲"""
        return bool(self._post("/api/player/play_song", {
            "song_info": {
                "id": song_id,
                "source": source
            }
        }))

    def search_and_play(self, keyword: str) -> Optional[Dict[str, Any]]:
        """搜索并播放第一首"""
        results = self.search(keyword)
        if results:
            song = results[0]
            if self.play_song(song.get("id", ""), song.get("source", "kw")):
                return song
        return None


def test_connection():
    """测试连接"""
    client = LxMusicClient()
    print(f"[测试] 连接落雪音乐: {client.base_url}")
    if client.is_connected():
        print("✅ 连接成功！")
        status = client.get_status()
        print(f"当前播放状态: {status}")
        return True
    else:
        print("❌ 连接失败，请确认落雪音乐已开启开放API")
        return False


if __name__ == "__main__":
    test_connection()
