"""下载功能：并发≤3、super→high 降级、入 music 表、已下载判定、失败不卡队列、
已存在不覆盖、.part 原子改名、无 Content-Length 进度。"""
import pytest

pytestmark = pytest.mark.unit


import os
import time
from unittest.mock import MagicMock, patch

from app.music.database import MusicDatabase
from app.music.downloader import DownloadManager
from app.music.online.models import OnlineTrack


def _mk_track(title="夜曲", artist="周杰伦", src="酷我音乐", tid="t1"):
    return OnlineTrack(source_name=src, track_id=tid, title=title, artist=artist,
                       album="", duration_seconds=200)


def _make_dl(tmp_path, max_workers=3):
    db = MusicDatabase(str(tmp_path / "t.db"))
    dl = DownloadManager(MagicMock(), db, str(tmp_path / "dl"), max_workers=max_workers)
    dl.online_manager = MagicMock()
    return dl, db


class _FakeResp:
    def __init__(self, chunks, total=None):
        self.headers = {"Content-Length": str(total)} if total is not None else {}
        self._chunks = list(chunks)
        self._i = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        if self._i >= len(self._chunks):
            return b""
        c = self._chunks[self._i]
        self._i += 1
        return c


class _SlowResp:
    def __init__(self, delay, total, chunk):
        self.headers = {"Content-Length": str(total)}
        self._delay = delay
        self._total = total
        self._chunk = chunk
        self._left = total

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        time.sleep(self._delay)
        if self._left <= 0:
            return b""
        c = self._chunk[:min(n, self._left)]
        self._left -= len(c)
        return c


def _url_result(url="http://x/song.flac"):
    return {"url": url, "headers": None}


def test_concurrent_workers_limited_to_3(tmp_path):
    """并发 ≤3：5 个慢下载同时活动数不超过 max_workers。"""
    dl, _db = _make_dl(tmp_path)
    dl.online_manager.search_all.side_effect = [
        {"酷我音乐": [_mk_track(title=f"歌{n}")]} for n in range(5)]
    dl.online_manager.get_play_url.return_value = _url_result("http://x/f.flac")
    with patch("app.music.downloader.urlopen", return_value=_SlowResp(0.15, 3000, b"x" * 1000)):
        events = {}
        for i in range(5):
            s = {"title": f"歌{i}", "artist": "A"}
            dl.download(s, {"on_progress": lambda k, p: None,
                            "on_done": lambda k, p: events.setdefault(k, "done"),
                            "on_fail": lambda k, r: events.setdefault(k, "fail")})
        peak = 0
        t0 = time.time()
        while time.time() - t0 < 4.0:
            peak = max(peak, dl.active_count())
            if len(events) >= 5:
                break
            time.sleep(0.03)
        assert peak <= 3, f"并发超限: peak={peak}"
        assert len(events) == 5, f"完成数不足: {len(events)}"


def test_quality_fallback_super_then_high(tmp_path):
    """super 失败 → 降 high 取 URL。"""
    dl, _db = _make_dl(tmp_path)
    dl.online_manager.search_all.return_value = {"酷我音乐": [_mk_track()]}
    dl.online_manager.get_play_url.side_effect = [None, _url_result("http://x/h.flac")]
    done = {}
    with patch("app.music.downloader.urlopen",
               return_value=_FakeResp([b"F" * 10], total=10)):
        dl.download({"title": "夜曲", "artist": "周杰伦"}, {
            "on_done": lambda k, p: done.setdefault(k, p),
            "on_fail": lambda k, r: done.setdefault(k, r),
        })
        time.sleep(0.5)
    qs = [c.args[1] if len(c.args) > 1 else c.kwargs.get("quality")
          for c in dl.online_manager.get_play_url.call_args_list]
    assert qs == ["super", "high"], f"降级顺序错误: {qs}"
    assert "夜曲|周杰伦" in done
    assert done["夜曲|周杰伦"].endswith(".flac")


def test_download_inserts_into_music_table(tmp_path):
    """下载完成 → 入 music 表 + 文件存在 + .part 无残留。"""
    dl, db = _make_dl(tmp_path)
    dl.online_manager.search_all.return_value = {"酷我音乐": [_mk_track()]}
    dl.online_manager.get_play_url.return_value = _url_result("http://x/song.flac")
    with patch("app.music.downloader.urlopen",
               return_value=_FakeResp([b"FLAC" * 20], total=80)):
        done = {}
        dl.download({"title": "夜曲", "artist": "周杰伦"},
                    {"on_done": lambda k, p: done.setdefault(k, p),
                     "on_fail": lambda k, r: done.setdefault(k, r)})
        time.sleep(0.5)
    assert "夜曲|周杰伦" in done
    final = done["夜曲|周杰伦"]
    assert os.path.exists(final)
    assert not final.endswith(".part")
    assert not os.path.exists(final + ".part")
    row = db.get_song_by_path(final)
    assert row is not None
    assert row["title"] == "夜曲" and row["artist"] == "周杰伦"


def test_downloaded_judgement(tmp_path):
    """已下载判定：下载目录有同名文件 → True；无 → False。"""
    dl, _db = _make_dl(tmp_path)
    assert dl.is_downloaded("夜曲", "周杰伦") is False
    os.makedirs(dl.download_dir, exist_ok=True)
    open(os.path.join(dl.download_dir, "夜曲 - 周杰伦.mp3"), "w").close()
    assert dl.is_downloaded("夜曲", "周杰伦") is True
    assert dl.is_downloaded("晴天", "周杰伦") is False


def test_failure_does_not_block_queue(tmp_path):
    """一个下载失败 → 其他任务继续完成（不卡队列）。"""
    dl, _db = _make_dl(tmp_path)
    # 第一个 track 重搜无结果（失败）；第二个成功
    dl.online_manager.search_all.side_effect = [
        {"酷我音乐": [_mk_track(title="坏歌")]},
        {"酷我音乐": [_mk_track(title="好歌")]},
    ]
    dl.online_manager.get_play_url.side_effect = [
        None, None,  # 坏歌 super+high 都失败
        _url_result("http://x/ok.flac"),
    ]
    with patch("app.music.downloader.urlopen",
               return_value=_FakeResp([b"x" * 10], total=10)):
        done = {}
        dl.download({"title": "坏歌", "artist": "A"},
                    {"on_done": lambda k, p: done.setdefault(k, "ok"),
                     "on_fail": lambda k, r: done.setdefault(k, "fail")})
        dl.download({"title": "好歌", "artist": "A"},
                    {"on_done": lambda k, p: done.setdefault(k, "ok"),
                     "on_fail": lambda k, r: done.setdefault(k, "fail")})
        time.sleep(0.8)
    assert done.get("坏歌|A") == "fail"
    assert done.get("好歌|A") == "ok"


def test_exists_skips_no_overwrite(tmp_path):
    """目标已存在 → on_exists 跳过，绝不覆盖。"""
    dl, _db = _make_dl(tmp_path)
    os.makedirs(dl.download_dir, exist_ok=True)
    target = os.path.join(dl.download_dir, "夜曲 - 周杰伦.flac")
    with open(target, "w", encoding="utf-8") as f:
        f.write("原始内容")
    dl.online_manager.search_all.return_value = {"酷我音乐": [_mk_track()]}
    dl.online_manager.get_play_url.return_value = _url_result("http://x/s.flac")
    with patch("app.music.downloader.urlopen",
               return_value=_FakeResp([b"NEWDATA" * 10], total=80)):
        evt = {}
        dl.download({"title": "夜曲", "artist": "周杰伦"},
                    {"on_exists": lambda k: evt.setdefault(k, "exists"),
                     "on_done": lambda k, p: evt.setdefault(k, "done")})
        time.sleep(0.5)
    assert "夜曲|周杰伦" in evt and evt["夜曲|周杰伦"] == "exists"
    with open(target, "r", encoding="utf-8") as f:
        assert f.read() == "原始内容"


def test_no_content_length_progress_none(tmp_path):
    """无 Content-Length → on_progress(key, None)，不卡不崩。"""
    dl, _db = _make_dl(tmp_path)
    dl.online_manager.search_all.return_value = {"酷我音乐": [_mk_track()]}
    dl.online_manager.get_play_url.return_value = _url_result("http://x/stream.m4a")
    with patch("app.music.downloader.urlopen",
               return_value=_FakeResp([b"MP4" * 30])):   # 无 Content-Length
        progress = []
        done = {}
        dl.download({"title": "夜曲", "artist": "周杰伦"},
                    {"on_progress": lambda k, p: progress.append(p),
                     "on_done": lambda k, p: done.setdefault(k, p),
                     "on_fail": lambda k, r: done.setdefault(k, r)})
        time.sleep(0.5)
    assert None in progress, "应回调 None 进度"
    assert "夜曲|周杰伦" in done
    assert done["夜曲|周杰伦"].endswith(".m4a")


def test_remove_download_deletes_file(tmp_path):
    """删除下载：删文件。"""
    dl, _db = _make_dl(tmp_path)
    os.makedirs(dl.download_dir, exist_ok=True)
    target = os.path.join(dl.download_dir, "夜曲 - 周杰伦.flac")
    with open(target, "w") as f:
        f.write("x")
    ok, path, err = dl.remove_download("夜曲", "周杰伦")
    assert ok and path == target
    assert not os.path.exists(target)
