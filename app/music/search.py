"""
音乐搜索 - 模糊搜索 + 相似度排序

第一版：SQLite FTS5 + BM25 + 字符串相似度
不使用向量数据库 / Embedding。
"""
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from .database import MusicDatabase


class MusicSearch:
    """音乐模糊搜索"""

    def __init__(self, db: MusicDatabase):
        self.db = db

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        综合搜索

        1. FTS5 全文搜索
        2. 字符串相似度重排序
        3. 兜底 LIKE 搜索
        """
        if not query or not query.strip():
            return []

        query = query.strip()

        # 1. FTS 搜索
        fts_results = self.db.search(query, limit=limit * 2)

        # 2. 相似度重排序
        scored = []
        for song in fts_results:
            score = self._calculate_similarity(query, song)
            song["search_score"] = score
            scored.append(song)

        # 按相似度排序
        scored.sort(key=lambda x: x.get("search_score", 0), reverse=True)

        # 3. 如果 FTS 结果太少，补充 LIKE 搜索
        if len(scored) < limit:
            like_results = self._like_search(query, limit=limit)
            existing_ids = {s["id"] for s in scored}
            for song in like_results:
                if song["id"] not in existing_ids:
                    song["search_score"] = self._calculate_similarity(query, song) * 0.5
                    scored.append(song)

        return scored[:limit]

    def search_by_artist(self, artist: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按歌手搜索"""
        query = f"%{artist}%"
        cursor = self.db._conn.execute("""
            SELECT * FROM music
            WHERE artist LIKE ? OR album_artist LIKE ?
            ORDER BY play_count DESC, last_played DESC
            LIMIT ?
        """, (query, query, limit))
        return [dict(row) for row in cursor.fetchall()]

    def search_by_album(self, album: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按专辑搜索"""
        query = f"%{album}%"
        cursor = self.db._conn.execute("""
            SELECT * FROM music
            WHERE album LIKE ?
            ORDER BY artist, track_number
            LIMIT ?
        """, (query, limit))
        return [dict(row) for row in cursor.fetchall()]

    def search_similar(self, song: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
        """
        搜索相似歌曲

        基于：同歌手、同专辑、同风格、标签相似度
        （第一版不使用音频特征，只基于元数据）
        """
        results = []
        artist = song.get("artist")
        genre = song.get("genre")
        album = song.get("album")
        song_id = song.get("id")

        # 同歌手的其他歌曲
        if artist:
            cursor = self.db._conn.execute("""
                SELECT * FROM music
                WHERE artist = ? AND id != ?
                ORDER BY play_count DESC
                LIMIT ?
            """, (artist, song_id, limit))
            for row in cursor.fetchall():
                s = dict(row)
                s["similar_score"] = 0.8
                results.append(s)

        # 同风格
        if genre and len(results) < limit:
            cursor = self.db._conn.execute("""
                SELECT * FROM music
                WHERE genre = ? AND id != ? AND artist != ?
                ORDER BY RANDOM()
                LIMIT ?
            """, (genre, song_id, artist or "", limit - len(results)))
            for row in cursor.fetchall():
                s = dict(row)
                s["similar_score"] = 0.4
                results.append(s)

        return results[:limit]

    def _like_search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """LIKE 模糊搜索兜底"""
        like_query = f"%{query}%"
        cursor = self.db._conn.execute("""
            SELECT * FROM music
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
            ORDER BY play_count DESC, last_played DESC
            LIMIT ?
        """, (like_query, like_query, like_query, limit))
        return [dict(row) for row in cursor.fetchall()]

    def _calculate_similarity(self, query: str, song: Dict[str, Any]) -> float:
        """
        计算查询与歌曲的相似度得分

        综合考虑：
        - 标题匹配度
        - 歌手匹配度
        - 专辑匹配度
        - 字符串编辑距离
        """
        scores = []

        title = (song.get("title") or "").lower()
        artist = (song.get("artist") or "").lower()
        album = (song.get("album") or "").lower()
        query_lower = query.lower()

        # 标题完全匹配
        if query_lower == title:
            scores.append(1.0)
        # 标题包含查询
        elif query_lower in title:
            scores.append(0.85)
        # 查询包含标题
        elif title and title in query_lower:
            scores.append(0.75)
        # 标题相似度
        elif title:
            scores.append(SequenceMatcher(None, query_lower, title).ratio() * 0.6)

        # 歌手匹配
        if artist:
            if query_lower == artist:
                scores.append(0.9)
            elif query_lower in artist:
                scores.append(0.7)
            elif artist in query_lower:
                scores.append(0.6)
            else:
                scores.append(SequenceMatcher(None, query_lower, artist).ratio() * 0.4)

        # 专辑匹配
        if album:
            if query_lower in album:
                scores.append(0.5)
            else:
                scores.append(SequenceMatcher(None, query_lower, album).ratio() * 0.2)

        # 取最高分
        return max(scores) if scores else 0.0

    def parse_search_intent(self, query: str) -> Dict[str, Any]:
        """
        解析搜索意图（规则匹配，不走 LLM）

        识别：
        - "播放 XXX 的歌" -> 按歌手
        - "XXX 专辑" -> 按专辑
        - "来一首 XXX" -> 按标题
        """
        result = {
            "type": "general",  # general / artist / album / title
            "query": query,
            "artist": None,
            "album": None,
            "title": None,
        }

        # 歌手模式："周杰伦的歌", "播放周杰伦", "周杰伦的"
        artist_patterns = [
            r"^(.+?)的歌$",
            r"^播放(.+?)(的歌)?$",
            r"^(.+?)的专辑$",
            r"^来一首(.+?)的",
        ]
        for pattern in artist_patterns:
            match = re.match(pattern, query)
            if match:
                artist = match.group(1).strip()
                if len(artist) >= 1:
                    result["type"] = "artist"
                    result["artist"] = artist
                    return result

        # 专辑模式
        album_patterns = [
            r"^(.+?)专辑$",
            r"^播放专辑(.+)$",
        ]
        for pattern in album_patterns:
            match = re.match(pattern, query)
            if match:
                result["type"] = "album"
                result["album"] = match.group(1).strip()
                return result

        return result
