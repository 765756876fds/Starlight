"""在线歌下载：重搜 → 取 URL（super→high 两层降级 + JS 链兜底）→ 流式写 .part → 入 music 表。

- 独立线程池（默认 ≤3 并发），不阻塞 UI、不碰 controller 的 mpv 实例。
- 文件名规范：歌名 - 歌手.<扩展名>（与扫描器解析格式一致；扩展名按 URL 后缀/Content-Type 定）。
- 目标文件已存在 → 跳过不覆盖（回调 on_exists）。
- .part 临时文件 → 成功后 os.replace 原子改名（防半截文件被当成品）。
- 无 Content-Length → on_progress(key, None)，UI 显示"下载中…"。
- 下载目录不存在 → 自动创建。
- 失败 → on_fail + 任务移除，不卡队列。
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_EXT_HINTS = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape")
_CT_EXTS = {
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ape": ".ape",
}
_ILLEGAL = '\\/:*?"<>|'


class DownloadManager:
    """并发受限的在线歌下载器。线程安全。"""

    def __init__(self, online_manager, db, download_dir, max_workers=3):
        self.online_manager = online_manager
        self.db = db
        self.download_dir = download_dir
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="music-dl")
        self._lock = threading.Lock()
        self._active = 0
        self._tasks = {}  # key -> {"status": queued/downloading/done/failed}

    # ---------- 判定 / 文件名 ----------

    @staticmethod
    def _key(title, artist):
        return f"{title or ''}|{artist or ''}".strip("|")

    @staticmethod
    def _sanitize(name: str) -> str:
        for ch in _ILLEGAL:
            name = name.replace(ch, "_")
        return name.strip()

    def _filename_base(self, title, artist) -> str:
        t = self._sanitize(title or "未知")
        a = self._sanitize(artist or "")
        return f"{t} - {a}" if a else t

    def is_downloaded(self, title, artist) -> bool:
        """下载目录里有 歌名 - 歌手.<任一音频扩展> → 已下载。"""
        base = self._filename_base(title, artist)
        if not base or not os.path.isdir(self.download_dir):
            return False
        for ext in _EXT_HINTS:
            if os.path.exists(os.path.join(self.download_dir, base + ext)):
                return True
        return False

    def find_download_path(self, title, artist) -> str:
        """返回已下载文件的完整路径；无 → ""。"""
        base = self._filename_base(title, artist)
        if not base or not os.path.isdir(self.download_dir):
            return ""
        for ext in _EXT_HINTS:
            p = os.path.join(self.download_dir, base + ext)
            if os.path.exists(p):
                return p
        return ""

    # ---------- 下载入口 ----------

    def download(self, song, callbacks=None) -> bool:
        """入队下载。返回 True=入队；False=重复/已存在（不覆盖）。"""
        title = song.get("title", "")
        artist = song.get("artist", "")
        key = self._key(title, artist)
        cb = callbacks or {}
        with self._lock:
            cur = self._tasks.get(key)
            if cur and cur["status"] in ("queued", "downloading"):
                return False
            if self.is_downloaded(title, artist):
                cb.get("on_exists", lambda k: None)(key)
                return False
            self._tasks[key] = {"status": "queued"}
        self._pool.submit(self._run, song, key, cb)
        return True

    def remove_download(self, title, artist) -> tuple:
        """删下载目录里的 歌名 - 歌手.* 文件。返回 (成功?, 路径, 错误信息)。"""
        path = self.find_download_path(title, artist)
        if not path:
            return False, "", "未找到已下载文件"
        try:
            os.remove(path)
            return True, path, ""
        except Exception as e:
            return False, path, str(e)

    # ---------- 内部 ----------

    def active_count(self) -> int:
        """当前运行中的下载数（测试/状态查询用）。"""
        with self._lock:
            return self._active

    def _run(self, song, key, cb):
        with self._lock:
            self._active += 1
        try:
            try:
                self._do_download(song, key, cb)
            except Exception as e:
                self._fail(key, cb, f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._active -= 1

    def _do_download(self, song, key, cb):
        # 1) 重搜（与播放链路一致：存 track_id 可能失效，按 title+artist 重搜）
        found = self._resolve_track(song)
        if not found:
            self._fail(key, cb, "在线未找到该歌")
            return
        track, url, headers = found
        # 2) 扩展名（URL 后缀优先 → Content-Type → 兜底 .mp3）
        ext = self._guess_ext(url, headers)
        # 3) 目标已存在 → 跳过，绝不覆盖
        base = self._filename_base(track.title, track.artist)
        final = os.path.join(self.download_dir, base + ext)
        if os.path.exists(final):
            cb.get("on_exists", lambda k: None)(key)
            return
        # 4) 目录不存在 → 自动创建；写 .part
        try:
            os.makedirs(self.download_dir, exist_ok=True)
        except Exception as e:
            self._fail(key, cb, f"无法创建下载目录: {e}")
            return
        part = final + ".part"
        got = self._stream(url, headers, part, key, cb)
        if got <= 0:
            self._cleanup(part)
            self._fail(key, cb, "下载内容为空")
            return
        # 5) 原子改名（防半截文件被当成品）
        try:
            os.replace(part, final)
        except Exception as e:
            self._cleanup(part)
            self._fail(key, cb, f"保存文件失败: {e}")
            return
        # 6) 入 music 表（不走全量重扫）
        try:
            self.db.insert_song({
                "path": final,
                "filename": os.path.basename(final),
                "title": track.title,
                "artist": track.artist,
                "album": getattr(track, "album", None),
                "duration": getattr(track, "duration_seconds", 0) or 0,
                "format": ext.lstrip("."),
                "filesize": got,
                "last_modified": time.time(),
            })
        except Exception as e:
            print(f"[Downloader] 入库失败: {e}")
        with self._lock:
            self._tasks[key]["status"] = "done"
        cb.get("on_done", lambda k, p: None)(key, final)

    def _resolve_track(self, song):
        """重搜：search_all → 展平 → 逐个 track 试 super→high URL，首个拿到的胜出。
        返回 (track, url, headers) 或 None。音质由 quality 档保证，源顺序只作 failover。"""
        query = f"{song.get('title', '')} {song.get('artist', '')}".strip()
        if not query or self.online_manager is None:
            return None
        try:
            d = self.online_manager.search_all(query, limit_per_source=5)
        except Exception as e:
            print(f"[Downloader] 重搜失败: {e}")
            return None
        all_tracks = [t for src in d.values() for t in src]
        for t in all_tracks:
            r = self._get_url_with_fallback(t)
            if r and r.get("url"):
                return t, r["url"], r.get("headers") or {}
        return None

    def _get_url_with_fallback(self, track):
        """super → high 两层 Python 降级（JS 层还有 want→high→standard→low 兜底）。"""
        for q in ("super", "high"):
            try:
                r = self.online_manager.get_play_url(track, quality=q)
                if r and r.get("url"):
                    return r
            except Exception:
                continue
        return None

    @staticmethod
    def _guess_ext(url, headers):
        try:
            path = urlparse(url).path.lower()
            for e in _EXT_HINTS:
                if path.endswith(e):
                    return e
        except Exception:
            pass
        try:
            ct = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct in _CT_EXTS:
                return _CT_EXTS[ct]
        except Exception:
            pass
        return ".mp3"

    def _stream(self, url, headers, part, key, cb):
        """流式下载到 .part；有 Content-Length → 百分比进度，无 → on_progress(key, None)。"""
        h = {"User-Agent": "Mozilla/5.0"}
        for k in ("Referer", "Cookie", "Authorization"):
            if headers and headers.get(k):
                h[k] = headers[k]
        req = Request(url, headers=h)
        with urlopen(req, timeout=30) as resp:
            total_s = resp.headers.get("Content-Length")
            total = int(total_s) if total_s and total_s.isdigit() else None
            got = 0
            with open(part, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        cb.get("on_progress", lambda k, p: None)(key, int(got * 100 / total))
                    else:
                        cb.get("on_progress", lambda k, p: None)(key, None)
        return got

    def _fail(self, key, cb, reason):
        with self._lock:
            self._tasks[key]["status"] = "failed"
        print(f"[Downloader] 下载失败: {key} -> {reason}")
        cb.get("on_fail", lambda k, r: None)(key, reason)

    @staticmethod
    def _cleanup(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
