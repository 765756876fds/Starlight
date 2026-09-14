"""
音乐数据库 - SQLite + FTS5 全文搜索

表结构：
- music: 音乐文件元数据
- music_fts: FTS5 全文索引（标题、歌手、专辑）
- music_highlight: 歌曲高潮位置（预留）
"""
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple


class MusicDatabase:
    """音乐数据库管理"""

    def __init__(self, db_path: str = "./data/music.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._init_tables()

    def _connect(self):
        """连接数据库"""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def _init_tables(self):
        """初始化表结构"""
        cursor = self._conn.cursor()

        # 音乐主表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS music (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                album TEXT,
                album_artist TEXT,
                track_number INTEGER,
                year INTEGER,
                genre TEXT,
                duration REAL,
                bitrate INTEGER,
                sample_rate INTEGER,
                channels INTEGER,
                filesize INTEGER,
                format TEXT,
                tags TEXT,
                last_modified REAL,
                added_at REAL,
                play_count INTEGER DEFAULT 0,
                last_played REAL
            )
        """)

        # FTS5 全文索引
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS music_fts USING fts5(
                title,
                artist,
                album,
                genre,
                content='music',
                content_rowid='id'
            )
        """)

        # 高潮位置表（预留）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS music_highlight (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                confidence REAL,
                source TEXT,
                created_at REAL,
                FOREIGN KEY (song_id) REFERENCES music(id) ON DELETE CASCADE
            )
        """)

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_music_artist ON music(artist)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_music_album ON music(album)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_music_title ON music(title)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_music_genre ON music(genre)")

        # FTS 触发器 - 自动同步
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS music_ai AFTER INSERT ON music BEGIN
                INSERT INTO music_fts(rowid, title, artist, album, genre)
                VALUES (new.id, new.title, new.artist, new.album, new.genre);
            END
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS music_ad AFTER DELETE ON music BEGIN
                INSERT INTO music_fts(music_fts, rowid, title, artist, album, genre)
                VALUES ('delete', old.id, old.title, old.artist, old.album, old.genre);
            END
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS music_au AFTER UPDATE ON music BEGIN
                INSERT INTO music_fts(music_fts, rowid, title, artist, album, genre)
                VALUES ('delete', old.id, old.title, old.artist, old.album, old.genre);
                INSERT INTO music_fts(rowid, title, artist, album, genre)
                VALUES (new.id, new.title, new.artist, new.album, new.genre);
            END
        """)

        self._conn.commit()

    def insert_song(self, song_data: Dict[str, Any]) -> int:
        """插入一首歌曲，返回 ID"""
        cursor = self._conn.cursor()
        now = time.time()

        # 如果已存在则更新
        existing = self.get_song_by_path(song_data["path"])
        if existing:
            self.update_song(existing["id"], song_data)
            return existing["id"]

        cursor.execute("""
            INSERT INTO music (
                path, filename, title, artist, album, album_artist,
                track_number, year, genre, duration, bitrate, sample_rate,
                channels, filesize, format, tags, last_modified, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            song_data.get("path"),
            song_data.get("filename"),
            song_data.get("title"),
            song_data.get("artist"),
            song_data.get("album"),
            song_data.get("album_artist"),
            song_data.get("track_number"),
            song_data.get("year"),
            song_data.get("genre"),
            song_data.get("duration"),
            song_data.get("bitrate"),
            song_data.get("sample_rate"),
            song_data.get("channels"),
            song_data.get("filesize"),
            song_data.get("format"),
            song_data.get("tags"),
            song_data.get("last_modified"),
            now,
        ))
        self._conn.commit()
        return cursor.lastrowid

    def update_song(self, song_id: int, song_data: Dict[str, Any]):
        """更新歌曲信息"""
        fields = []
        values = []
        for key in ["title", "artist", "album", "album_artist", "track_number",
                     "year", "genre", "duration", "bitrate", "sample_rate",
                     "channels", "filesize", "format", "tags", "last_modified"]:
            if key in song_data:
                fields.append(f"{key} = ?")
                values.append(song_data[key])

        if fields:
            values.append(song_id)
            self._conn.execute(
                f"UPDATE music SET {', '.join(fields)} WHERE id = ?",
                values
            )
            self._conn.commit()

    def get_song_by_path(self, path: str) -> Optional[Dict[str, Any]]:
        """按路径获取歌曲"""
        cursor = self._conn.execute("SELECT * FROM music WHERE path = ?", (path,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_song_by_id(self, song_id: int) -> Optional[Dict[str, Any]]:
        """按 ID 获取歌曲"""
        cursor = self._conn.execute("SELECT * FROM music WHERE id = ?", (song_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        FTS5 全文搜索 + BM25 排序

        支持中文搜索，使用简单的分词策略。
        """
        if not query or not query.strip():
            return []

        # 构造 FTS 查询 - 对中文做字符级匹配
        # 先用 BM25 搜索
        try:
            # 尝试直接搜索
            fts_query = self._build_fts_query(query)
            cursor = self._conn.execute("""
                SELECT m.*, bm25(music_fts) as rank
                FROM music_fts
                JOIN music m ON m.id = music_fts.rowid
                WHERE music_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit))
            results = [dict(row) for row in cursor.fetchall()]
            if results:
                return results
        except Exception:
            pass

        # FTS 失败时用 LIKE 模糊匹配兜底
        like_query = f"%{query}%"
        cursor = self._conn.execute("""
            SELECT * FROM music
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
            ORDER BY play_count DESC, last_played DESC
            LIMIT ?
        """, (like_query, like_query, like_query, limit))
        return [dict(row) for row in cursor.fetchall()]

    def _build_fts_query(self, query: str) -> str:
        """构造 FTS5 查询表达式"""
        # 移除特殊字符
        clean = query.strip().replace('"', '').replace("'", "")
        # 对中文按字符拆分，英文按空格拆分
        terms = []
        current_word = ""
        for char in clean:
            if '\u4e00' <= char <= '\u9fff':
                if current_word:
                    terms.append(current_word)
                    current_word = ""
                terms.append(char)
            elif char.isspace():
                if current_word:
                    terms.append(current_word)
                    current_word = ""
            else:
                current_word += char
        if current_word:
            terms.append(current_word)

        # 用 OR 连接（宽松匹配），用引号包裹中文单字
        parts = []
        for term in terms:
            if len(term) == 1 and '\u4e00' <= term <= '\u9fff':
                parts.append(f'"{term}"')
            else:
                parts.append(f'"{term}"')

        return " OR ".join(parts) if parts else query

    def get_all_songs(self, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
        """获取所有歌曲"""
        cursor = self._conn.execute(
            "SELECT * FROM music ORDER BY artist, album, track_number LIMIT ? OFFSET ?",
            (limit, offset)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_artists(self) -> List[Dict[str, Any]]:
        """获取所有歌手"""
        cursor = self._conn.execute("""
            SELECT artist, COUNT(*) as song_count,
                   COUNT(DISTINCT album) as album_count
            FROM music
            WHERE artist IS NOT NULL AND artist != ''
            GROUP BY artist
            ORDER BY song_count DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_albums(self) -> List[Dict[str, Any]]:
        """获取所有专辑"""
        cursor = self._conn.execute("""
            SELECT album, artist, COUNT(*) as song_count,
                   MIN(year) as year
            FROM music
            WHERE album IS NOT NULL AND album != ''
            GROUP BY album, artist
            ORDER BY artist, year
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_random_songs(self, count: int = 10) -> List[Dict[str, Any]]:
        """随机获取歌曲"""
        cursor = self._conn.execute(
            "SELECT * FROM music ORDER BY RANDOM() LIMIT ?", (count,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def increment_play_count(self, song_id: int):
        """增加播放计数"""
        self._conn.execute("""
            UPDATE music SET play_count = play_count + 1, last_played = ?
            WHERE id = ?
        """, (time.time(), song_id))
        self._conn.commit()

    def delete_song(self, song_id: int):
        """删除歌曲"""
        self._conn.execute("DELETE FROM music WHERE id = ?", (song_id,))
        self._conn.commit()

    def get_song_count(self) -> int:
        """获取歌曲总数"""
        cursor = self._conn.execute("SELECT COUNT(*) as cnt FROM music")
        return cursor.fetchone()["cnt"]

    def get_highlight(self, song_id: int) -> Optional[Dict[str, Any]]:
        """获取歌曲高潮位置"""
        cursor = self._conn.execute(
            "SELECT * FROM music_highlight WHERE song_id = ? ORDER BY confidence DESC LIMIT 1",
            (song_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def save_highlight(self, song_id: int, start_time: float,
                        end_time: Optional[float] = None,
                        confidence: float = 1.0, source: str = "manual"):
        """保存歌曲高潮位置"""
        self._conn.execute("""
            INSERT INTO music_highlight (song_id, start_time, end_time, confidence, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (song_id, start_time, end_time, confidence, source, time.time()))
        self._conn.commit()

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None
