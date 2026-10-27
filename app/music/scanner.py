"""
音乐文件扫描器 - 扫描本地音乐文件并提取元数据

使用 mutagen 读取 ID3/FLAC/MP4 等标签。
支持增量扫描（根据文件修改时间判断是否需要更新）。
"""
import logging
import os
import shutil
import subprocess
import threading
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

from .database import MusicDatabase
from .highlight_detector import (
    detect_highlight,
    get_source_tag,
    pop_last_source,
)


class MusicScanner:
    """音乐文件扫描器"""

    SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".ape"}

    VERIFY_TIMEOUT = 8.0  # 可播验证：mpv 进程超时秒数

    def __init__(self, db: MusicDatabase, scan_dirs: list[str],
                 extensions: set[str] | None = None,
                 mpv_path: str | None = None):
        self.db = db
        self.scan_dirs = scan_dirs
        self.extensions = extensions or self.SUPPORTED_EXTENSIONS
        self.mpv_path = mpv_path
        # 可播验证可用性（mpv 不存在/启动失败 → 降级跳过验证，照常入库）
        self._verify_mpv_ok = self._resolve_mpv_path()
        self._scanned_count = 0
        self._added_count = 0
        self._updated_count = 0
        self._skipped_count = 0
        self._error_count = 0
        self._unplayable_count = 0
        self._highlight_running = False  # 防止重复启动worker
        # 保护 _highlight_running 的锁：start_highlight_background 的"检查→置位→起线程"
        # 必须原子化，否则两个调用方可能同时读到 False 并各起一个 worker。
        self._highlight_start_lock = threading.Lock()

    def scan(self, full_rescan: bool = False) -> dict[str, int]:
        """
        执行扫描

        Args:
            full_rescan: 是否完全重新扫描（忽略修改时间）

        Returns:
            扫描统计
        """
        self._scanned_count = 0
        self._added_count = 0
        self._updated_count = 0
        self._skipped_count = 0
        self._error_count = 0
        self._unplayable_count = 0
        # 注意：不要在这里重置 _highlight_running。
        # 启动顺序：controller.start() 先调 start_highlight_background()，随后 main.py
        # 调 rescan() → scan() → start_highlight_background()。若此处把 running 置 False，
        # 第二个调用会再启一个并发 worker，两个 worker 写同一首歌产生重复行。
        # worker 自己在 finally 里复位 _highlight_running。

        all_files = self._collect_files()

        for filepath in all_files:
            self._scanned_count += 1
            try:
                self._process_file(filepath, full_rescan)
            except Exception as e:
                self._error_count += 1
                print(f"[Scanner] 处理文件失败 {filepath}: {e}")

        # 扫描完成：WAL checkpoint，防 -wal 无限膨胀（写放大集中在扫描）
        try:
            self.db.checkpoint()
        except Exception as e:
            print(f"[Scanner] WAL checkpoint 失败: {e}")

        return {
            "scanned": self._scanned_count,
            "added": self._added_count,
            "updated": self._updated_count,
            "skipped": self._skipped_count,
            "errors": self._error_count,
            "unplayable": self._unplayable_count,
        }

    def _collect_files(self) -> list[str]:
        """收集所有音乐文件"""
        files = []
        for scan_dir in self.scan_dirs:
            if not os.path.exists(scan_dir):
                print(f"[Scanner] 目录不存在: {scan_dir}")
                continue
            for root, _, filenames in os.walk(scan_dir):
                for filename in filenames:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in self.extensions:
                        files.append(os.path.join(root, filename))
        return files

    def _process_file(self, filepath: str, full_rescan: bool):
        """处理单个文件"""
        try:
            stat = os.stat(filepath)
        except OSError:
            self._error_count += 1
            return

        last_modified = stat.st_mtime
        filesize = stat.st_size

        # 检查是否已存在且未修改
        existing = self.db.get_song_by_path(filepath)
        if existing and not full_rescan:
            if abs(existing.get("last_modified", 0) - last_modified) < 1:
                self._skipped_count += 1
                return

        # 可播验证：只对新增/变更文件（mpv 不可用时降级照常入库）
        if self._verify_mpv_ok and not self._verify_playable(filepath):
            self._unplayable_count += 1
            print(f"[Scanner] 不可播文件已跳过入库: {filepath}")
            return

        # 提取元数据
        song_data = self._extract_metadata(filepath)
        song_data["path"] = filepath
        song_data["filename"] = os.path.basename(filepath)
        song_data["filesize"] = filesize
        song_data["last_modified"] = last_modified
        song_data["format"] = os.path.splitext(filepath)[1].lower().lstrip(".")

        if existing:
            self.db.update_song(existing["id"], song_data)
            self._updated_count += 1
        else:
            self.db.insert_song(song_data)
            self._added_count += 1

    def _resolve_mpv_path(self) -> bool:
        """解析可播验证用的 mpv 路径；不可用返回 False（降级跳过验证）。"""
        if not self.mpv_path:
            return False
        if os.path.isfile(self.mpv_path):
            return True
        try:
            found = shutil.which(self.mpv_path)
            if found:
                self.mpv_path = found
                return True
        except Exception:
            pass
        print(f"[Scanner] mpv 不存在: {self.mpv_path}，可播验证降级（照常入库）")
        return False

    def _verify_playable(self, filepath: str) -> bool:
        """独立 mpv 进程试播验证（--frames=1，绝不碰 controller 的 mpv 实例）。

        exit=0 → 可播；非 0 / 超时（VERIFY_TIMEOUT 秒，杀进程）→ 不可播；
        验证本身异常 → 降级返回 True（照常入库，不因验证坏掉扫描）。
        """
        try:
            proc = subprocess.Popen(
                [self.mpv_path, "--no-config", "--no-video", "--vo=null",
                 "--ao=null", "--frames=1", "--msg-level=all=error", "--", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,  # 关黑窗：试播 mpv 不弹控制台
            )
            try:
                rc = proc.wait(timeout=self.VERIFY_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
                print(f"[Scanner] 可播验证超时({self.VERIFY_TIMEOUT}s)，判定不可播: {filepath}")
                return False
            if rc == 0:
                return True
            print(f"[Scanner] 可播验证失败 (mpv exit={rc}): {filepath}")
            return False
        except Exception as e:
            print(f"[Scanner] 可播验证异常（降级照常入库）: {e!r}")
            return True

    def _extract_metadata(self, filepath: str) -> dict[str, Any]:
        """
        从音乐文件提取元数据

        优先使用 mutagen，失败时从文件名解析。
        """
        data = {
            "title": None,
            "artist": None,
            "album": None,
            "album_artist": None,
            "track_number": None,
            "year": None,
            "genre": None,
            "duration": None,
            "bitrate": None,
            "sample_rate": None,
            "channels": None,
            "tags": None,
        }

        if MUTAGEN_AVAILABLE:
            try:
                audio = MutagenFile(filepath)
                if audio is not None:
                    # 时长
                    if hasattr(audio, "info") and audio.info:
                        data["duration"] = getattr(audio.info, "length", None)
                        data["bitrate"] = getattr(audio.info, "bitrate", None)
                        data["sample_rate"] = getattr(audio.info, "sample_rate", None)
                        data["channels"] = getattr(audio.info, "channels", None)

                    # 标签
                    tags = audio.tags
                    if tags:
                        self._parse_tags(tags, data, filepath)
            except Exception as e:
                print(f"[Scanner] mutagen 读取失败 {filepath}: {e}")

        # 从文件名解析兜底（文件名为"歌名 - 歌手"，歌名在前）
        parts = self._parse_name_parts(filepath)
        if not data["title"]:
            data["title"] = parts[0]
        if not data["artist"] and len(parts) == 2:
            data["artist"] = parts[1]

        return data

    def _parse_tags(self, tags, data: dict[str, Any], filepath: str):
        """解析各种格式的标签"""
        ext = os.path.splitext(filepath)[1].lower()

        if ext == ".flac":
            # FLAC 使用 Vorbis 注释
            data["title"] = self._get_tag(tags, "title")
            data["artist"] = self._get_tag(tags, "artist")
            data["album"] = self._get_tag(tags, "album")
            data["album_artist"] = self._get_tag(tags, "albumartist")
            data["genre"] = self._get_tag(tags, "genre")
            data["year"] = self._parse_year(self._get_tag(tags, "date") or self._get_tag(tags, "year"))
            data["track_number"] = self._parse_track_number(self._get_tag(tags, "tracknumber"))

        elif ext == ".mp3":
            # MP3 使用 ID3
            data["title"] = self._get_id3_tag(tags, "TIT2")
            data["artist"] = self._get_id3_tag(tags, "TPE1")
            data["album"] = self._get_id3_tag(tags, "TALB")
            data["album_artist"] = self._get_id3_tag(tags, "TPE2")
            data["genre"] = self._get_id3_tag(tags, "TCON")
            data["year"] = self._parse_year(
                self._get_id3_tag(tags, "TDRC") or self._get_id3_tag(tags, "TYER")
            )
            data["track_number"] = self._parse_track_number(self._get_id3_tag(tags, "TRCK"))

        elif ext in (".m4a", ".mp4"):
            # M4A/MP4 使用 iTunes 风格
            data["title"] = self._get_mp4_tag(tags, "\xa9nam")
            data["artist"] = self._get_mp4_tag(tags, "\xa9ART")
            data["album"] = self._get_mp4_tag(tags, "\xa9alb")
            data["album_artist"] = self._get_mp4_tag(tags, "aART")
            data["genre"] = self._get_mp4_tag(tags, "\xa9gen")
            data["year"] = self._parse_year(self._get_mp4_tag(tags, "\xa9day"))
            data["track_number"] = self._parse_mp4_track(tags)

        elif ext == ".ogg":
            data["title"] = self._get_tag(tags, "title")
            data["artist"] = self._get_tag(tags, "artist")
            data["album"] = self._get_tag(tags, "album")
            data["genre"] = self._get_tag(tags, "genre")
            data["year"] = self._parse_year(self._get_tag(tags, "date"))

    def _get_tag(self, tags, key: str) -> str | None:
        """获取 Vorbis 风格标签"""
        values = tags.get(key)
        if values and len(values) > 0:
            return str(values[0]).strip()
        return None

    def _get_id3_tag(self, tags, key: str) -> str | None:
        """获取 ID3 标签"""
        if key in tags:
            tag = tags[key]
            if hasattr(tag, "text") and tag.text:
                return str(tag.text[0]).strip()
        return None

    def _get_mp4_tag(self, tags, key: str) -> str | None:
        """获取 MP4 标签"""
        values = tags.get(key)
        if values and len(values) > 0:
            return str(values[0]).strip()
        return None

    def _parse_mp4_track(self, tags) -> int | None:
        """解析 MP4 轨道号"""
        trkn = tags.get("trkn")
        if trkn and len(trkn) > 0:
            try:
                return trkn[0][0]
            except (IndexError, TypeError):
                pass
        return None

    def _parse_year(self, value: str | None) -> int | None:
        """解析年份"""
        if not value:
            return None
        try:
            # 处理 "2023-01-01" 或 "2023"
            return int(str(value)[:4])
        except (ValueError, TypeError):
            return None

    def _parse_track_number(self, value: str | None) -> int | None:
        """解析轨道号"""
        if not value:
            return None
        try:
            # 处理 "3/12" 或 "3"
            return int(str(value).split("/")[0])
        except (ValueError, TypeError):
            return None

    def _parse_name_parts(self, filepath: str) -> list[str]:
        """从文件名解析"歌名 - 歌手"，返回 [歌名, 歌手]；无 " - " 时返回 [整名]"""
        filename = os.path.basename(filepath)
        name, _ = os.path.splitext(filename)
        return [p.strip() for p in name.rsplit(" - ", 1)]

    def _parse_title_from_filename(self, filepath: str) -> str:
        """从文件名解析标题（文件名为"歌名 - 歌手"，歌名在前）"""
        parts = self._parse_name_parts(filepath)
        return parts[0]

    def start_highlight_background(self):
        """扫描完成后启动后台线程计算所有未处理歌曲的 highlight。

        用 _highlight_start_lock 把"检查 running → 置位 → 起线程"做成临界区，
        防止两个调用方并发穿透。worker 在 finally 里复位 running，且只在
        running 仍指向自己这一轮时才复位（防止上一个 worker 已退出后被
        新 worker 误清）。
        """
        with self._highlight_start_lock:
            if self._highlight_running:
                logger.debug("Highlight worker 已在运行，跳过重复启动")
                return
            self._highlight_running = True
            self._highlight_gen = getattr(self, "_highlight_gen", 0) + 1
            my_gen = self._highlight_gen
            thread = threading.Thread(
                target=self._highlight_worker,
                args=(my_gen,),
                name="HighlightWorker",
                daemon=True,
            )
            thread.start()
            logger.info("后台Highlight计算线程已启动 (gen=%d)", my_gen)

    def _highlight_worker(self, my_gen: int = 0):
        """后台逐首计算highlight，单线程低优先级。

        迁移语义（2026-10 V4 恢复后）：
        - manual 记录永远跳过，不删不写。
        - 已有当前算法（V4）记录的歌曲跳过。
        - 其余歌曲（无 highlight、或旧 pychorus 记录）走 V4 计算；
          V4 成功后用 replace_highlight_atomically 替换旧非 manual 记录；
          V4 失败则保留旧 pychorus 记录，不删。
        """
        try:
            current_tag = get_source_tag()  # 'local_v4_early_rise'
            all_songs = self.db.get_all_songs(limit=10000)
            pending = []
            for song in all_songs:
                sid = song["id"]
                # manual 存在即跳过（不依赖 get_highlight 的 top-1）
                if self.db.has_manual_highlight(sid):
                    continue
                existing = self.db.get_highlight(sid)
                # 已有当前算法记录的就跳过
                if existing and existing.get("source") == current_tag:
                    continue
                pending.append(song)

            if not pending:
                logger.info("Highlight: 所有歌曲都已计算，跳过")
                return

            logger.info(f"Highlight: 开始后台计算 {len(pending)} 首歌曲（算法={current_tag}）")
            success = 0
            failed = 0

            for song in pending:
                try:
                    # 双重检查：pending 列表是 worker 启动时拍的快照。如果期间
                    # 另一个 worker/手动标记已经写了结果，就不要重复写。
                    if self.db.has_manual_highlight(song["id"]):
                        continue
                    now_existing = self.db.get_highlight(song["id"])
                    if now_existing and now_existing.get("source") == current_tag:
                        continue

                    result = detect_highlight(song["path"])
                    if result:
                        start, end, score = result
                        # 原子替换：失败抛异常 → 走 except，旧记录保留
                        self.db.replace_highlight_atomically(
                            song_id=song["id"],
                            start_time=start,
                            end_time=end,
                            confidence=score,
                            source=pop_last_source(),
                        )
                        success += 1
                    else:
                        # V4 没出结果：保留旧 pychorus 记录，不删
                        failed += 1
                except Exception as e:
                    logger.warning(f"Highlight计算失败 {song.get('title', '')}: {e}")
                    failed += 1

            logger.info(f"Highlight后台计算完成: 成功{success}, 失败{failed}, 跳过{len(all_songs)-len(pending)}")
        except Exception as e:
            logger.error(f"Highlight后台线程异常: {e}")
        finally:
            # 只在 gen 仍是自己时才复位，防止上一个 worker 退出后被新 worker 误清
            with self._highlight_start_lock:
                if getattr(self, "_highlight_gen", None) == my_gen:
                    self._highlight_running = False

