"""
音乐数据库 - SQLite + FTS5 全文搜索

表结构：
- music: 音乐文件元数据
- music_fts: FTS5 全文索引（标题、歌手、专辑）
- music_highlight: 歌曲高潮位置（预留）
"""
import os
import sqlite3
import threading
import time
from typing import Any


class MusicDatabase:
    """音乐数据库管理"""

    def __init__(self, db_path: str = "./data/music.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        # 连接级互斥：scanner 后台 highlight 线程与 playback/UI 线程共享同一连接
        # （check_same_thread=False + WAL 只保证读写不互斥，写写并发仍需应用层锁）
        # RLock：close() 先调 checkpoint()（内部拿锁），再 with self._lock——
        # 顺序获取本不死锁，但用可重入锁防御未来"锁内再拿锁"的嵌套调用
        self._lock = threading.RLock()
        try:
            self._connect()
            self._init_tables()
        except sqlite3.DatabaseError as e:
            # 数据库损坏：不直接删除（数据安全）→ 重命名保留现场 + 新建空库
            print(f"[DB] 数据库打开失败（可能损坏）: {e!r}")
            # 关键：sqlite3.connect 可能已持有损坏文件句柄（PRAGMA/CREATE 才失败），
            # 必须先关闭连接再 rename，否则 Windows 下文件被占用必现 PermissionError
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._recover_corrupt()
            self._connect()
            self._init_tables()

    def _recover_corrupt(self) -> None:
        """损坏 db 恢复：重命名 db.corrupt.<时间戳> 保留现场，新建空库。

        WAL 模式在主 db 旁伴生 -wal / -shm 文件：Windows 下 sqlite3 连接
        close() 会顺手清理无效伴生日志（实测）；其他平台未必。故循环仍
        逐一检查 rename（跨平台兜底），保证旧 WAL 不残留干扰新库。
        不直接删除损坏文件（便于用户事后恢复/排查）；重命名失败（文件
        被锁）属极端场景，抛出让上层看到真实原因。
        """
        ts = time.strftime("%Y%m%d_%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            p = f"{self.db_path}{suffix}"
            if os.path.exists(p):
                backup = f"{p}.corrupt.{ts}"
                try:
                    os.rename(p, backup)
                    print(f"[DB] 已备份: {backup}（请重新扫描音乐库）")
                except Exception as e:
                    raise RuntimeError(
                        f"[DB] 备份损坏数据库文件失败（文件可能被占用）: {e!r}") from e

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

        # 播放历史（只存元数据，不缓存歌曲文件）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS play_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                artist TEXT DEFAULT '',
                source_type TEXT NOT NULL,
                source_name TEXT DEFAULT '',
                track_id TEXT DEFAULT '',
                music_id INTEGER DEFAULT 0,
                played_at REAL NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_play_history_played_at ON play_history(played_at DESC)")

        # 歌单（只存元数据，不缓存歌曲文件；URL 有时效，在线歌只存 source_name + track_id，播放时重搜）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS playlist_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist TEXT DEFAULT '',
                source_type TEXT NOT NULL,
                source_name TEXT DEFAULT '',
                track_id TEXT DEFAULT '',
                music_id INTEGER DEFAULT 0,
                added_at REAL NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_playlist_songs_playlist ON playlist_songs(playlist_id)")

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

    def insert_song(self, song_data: dict[str, Any]) -> int:
        """插入一首歌曲，返回 ID"""
        with self._lock:
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

    def update_song(self, song_id: int, song_data: dict[str, Any]):
        """更新歌曲信息"""
        with self._lock:
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

    def get_song_by_path(self, path: str) -> dict[str, Any] | None:
        """按路径获取歌曲"""
        cursor = self._conn.execute("SELECT * FROM music WHERE path = ?", (path,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_song_by_id(self, song_id: int) -> dict[str, Any] | None:
        """按 ID 获取歌曲"""
        cursor = self._conn.execute("SELECT * FROM music WHERE id = ?", (song_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
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

    def get_all_songs(self, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        """获取所有歌曲"""
        cursor = self._conn.execute(
            "SELECT * FROM music ORDER BY artist, album, track_number LIMIT ? OFFSET ?",
            (limit, offset)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_artists(self) -> list[dict[str, Any]]:
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

    def get_albums(self) -> list[dict[str, Any]]:
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

    def get_random_songs(self, count: int = 10) -> list[dict[str, Any]]:
        """随机获取歌曲"""
        cursor = self._conn.execute(
            "SELECT * FROM music ORDER BY RANDOM() LIMIT ?", (count,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def increment_play_count(self, song_id: int):
        """增加播放计数"""
        with self._lock:
            self._conn.execute("""
                UPDATE music SET play_count = play_count + 1, last_played = ?
                WHERE id = ?
            """, (time.time(), song_id))
            self._conn.commit()

    def delete_song(self, song_id: int):
        """删除歌曲"""
        with self._lock:
            self._conn.execute("DELETE FROM music WHERE id = ?", (song_id,))
            self._conn.commit()

    def delete_missing_songs(self) -> dict[str, int]:
        """删除文件已不存在的歌曲记录（连同其 highlight），返回删除数量。
        扫描器只增不删，文件被移动/删除后旧记录会一直残留并导致播放跳过。"""
        with self._lock:
            cursor = self._conn.execute("SELECT id, path FROM music")
            rows = cursor.fetchall()
            missing_ids = [r["id"] for r in rows
                           if not r["path"] or not os.path.exists(r["path"])]
            if missing_ids:
                self._conn.executemany(
                    "DELETE FROM music_highlight WHERE song_id = ?",
                    [(i,) for i in missing_ids])
                self._conn.executemany(
                    "DELETE FROM music WHERE id = ?", [(i,) for i in missing_ids])
                self._conn.commit()
            return {"deleted": len(missing_ids)}

    def get_song_count(self) -> int:
        """获取歌曲总数"""
        cursor = self._conn.execute("SELECT COUNT(*) as cnt FROM music")
        return cursor.fetchone()["cnt"]

    def get_highlight(self, song_id: int) -> dict[str, Any] | None:
        """获取歌曲高潮位置"""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM music_highlight WHERE song_id = ? ORDER BY confidence DESC LIMIT 1",
                (song_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def save_highlight(self, song_id: int, start_time: float,
                        end_time: float | None = None,
                        confidence: float = 1.0, source: str = "manual"):
        """保存歌曲高潮位置"""
        with self._lock:
            self._conn.execute("""
                INSERT INTO music_highlight (song_id, start_time, end_time, confidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (song_id, float(start_time),
                  float(end_time) if end_time is not None else None,
                  float(confidence), source, time.time()))
            self._conn.commit()

    def delete_highlights(self, song_id: int):
        """删除某首歌的全部高潮记录（重扫/换源前清理，避免新旧版本混存）"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM music_highlight WHERE song_id = ?", (song_id,))
            self._conn.commit()

    def checkpoint(self):
        """WAL checkpoint：把 WAL 内容合并回主库并截断（防 -wal 无限膨胀）。

        扫描是主要写入源（全量 upsert + FTS 同步 + highlight），扫描完成后调用
        一次即收干净；进程退出（close）前再兜底一次。
        """
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                print(f"[DB] WAL checkpoint 失败: {e}")

    def add_history(self, title: str, artist: str, source_type: str,
                    source_name: str = "", track_id: str = "",
                    music_id: int = 0, played_at: float | None = None) -> int:
        """写入一条播放历史（只存元数据，不缓存歌曲文件）；超过 200 条删最旧。"""
        with self._lock:
            now = played_at if played_at is not None else time.time()
            cursor = self._conn.execute(
                "INSERT INTO play_history "
                "(title, artist, source_type, source_name, track_id, music_id, played_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, artist, source_type, source_name, track_id, int(music_id), now))
            row_id = cursor.lastrowid
            # 上限 200：删除最旧的超量行
            self._conn.execute(
                "DELETE FROM play_history WHERE id IN ("
                "SELECT id FROM play_history ORDER BY played_at DESC LIMIT -1 OFFSET 200)")
            self._conn.commit()
            return int(row_id)

    def get_history(self, limit: int = 200) -> list[dict[str, Any]]:
        """播放历史（最近在前）。"""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM play_history ORDER BY played_at DESC LIMIT ?",
                (int(limit),))
            return [dict(row) for row in cursor.fetchall()]

    def clear_history(self):
        """清空播放历史。"""
        with self._lock:
            self._conn.execute("DELETE FROM play_history")
            self._conn.commit()

    # ---- 歌单 ----

    def create_playlist(self, name: str) -> int | None:
        """新建歌单；名字已存在（UNIQUE 冲突）返回 None。"""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO playlists (name, created_at) VALUES (?, ?)",
                    (name, time.time()))
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def delete_playlist(self, playlist_id: int):
        """删除歌单：显式级联删 playlist_songs（不依赖 foreign_keys pragma）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM playlist_songs WHERE playlist_id = ?", (playlist_id,))
            self._conn.execute(
                "DELETE FROM playlists WHERE id = ?", (playlist_id,))
            self._conn.commit()

    def rename_playlist(self, playlist_id: int, name: str) -> bool:
        """重命名；名字已存在（UNIQUE 冲突）返回 False。"""
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_playlists(self) -> list[dict[str, Any]]:
        """全部歌单（按创建时间），附 song_count。"""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM playlist_songs s"
                " WHERE s.playlist_id = p.id) AS song_count"
                " FROM playlists p ORDER BY p.created_at")
            return [dict(r) for r in cursor.fetchall()]

    def add_to_playlist(self, playlist_id: int, song: dict[str, Any]) -> bool:
        """加歌到歌单；同歌单内同歌去重（本地按 music_id、在线按 source_name+track_id），
        重复返回 False。"""
        with self._lock:
            # 本地歌 dict 常无 source_type 键：缺失/空一律按 local（在线歌显式传 "online"）
            st = song.get("source_type") or "local"
            title = song.get("title") or ""
            artist = song.get("artist") or ""
            sn = song.get("source_name") or ""
            tid = str(song.get("track_id") or "")
            # 本地列表项原始 dict 常只带 "id"（无 music_id）：双键 fallback，防御直调
            mid = int(song.get("music_id") or song.get("id") or 0)
            if st == "local":
                dup = self._conn.execute(
                    "SELECT id FROM playlist_songs"
                    " WHERE playlist_id = ? AND source_type = 'local' AND music_id = ?",
                    (playlist_id, mid)).fetchone()
            else:
                dup = self._conn.execute(
                    "SELECT id FROM playlist_songs"
                    " WHERE playlist_id = ? AND source_type = 'online'"
                    " AND source_name = ? AND track_id = ?",
                    (playlist_id, sn, tid)).fetchone()
            if dup:
                return False
            self._conn.execute(
                "INSERT INTO playlist_songs"
                " (playlist_id, title, artist, source_type, source_name,"
                " track_id, music_id, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (playlist_id, title, artist, st, sn, tid, mid, time.time()))
            self._conn.commit()
            return True

    def remove_from_playlist(self, playlist_id: int, song_id: int) -> bool:
        """从歌单移除（song_id 为 playlist_songs.id）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM playlist_songs WHERE id = ? AND playlist_id = ?",
                (song_id, playlist_id))
            self._conn.commit()
            return cur.rowcount > 0

    def get_playlist_songs(self, playlist_id: int) -> list[dict[str, Any]]:
        """歌单内歌曲（按加入时间）。"""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM playlist_songs WHERE playlist_id = ? ORDER BY added_at",
                (playlist_id,))
            return [dict(r) for r in cursor.fetchall()]

    def delete_playlist_songs_by_music_id(self, music_id: int):
        """按 music_id 从所有歌单移除（删本地文件时同步清理引用）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM playlist_songs WHERE music_id = ?", (music_id,))
            self._conn.commit()

    def close(self):
        """关闭数据库连接（先 checkpoint 落盘）"""
        try:
            self.checkpoint()
        except Exception:
            pass
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
