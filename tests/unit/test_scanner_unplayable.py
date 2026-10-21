"""扫描可播验证测试：坏文件不入库 / 正常入库 / mpv 降级 / 超时不死等。"""
import pytest

pytestmark = pytest.mark.unit

import time
from subprocess import TimeoutExpired
from unittest.mock import patch

from app.music.database import MusicDatabase
from app.music.scanner import MusicScanner


def _mk(tmp_path, mpv_path=None):
    db_path = str(tmp_path / "scan.db")
    db = MusicDatabase(db_path)
    if mpv_path is None:
        fake = tmp_path / "mpv.exe"
        fake.write_bytes(b"")
        mpv_path = str(fake)
    sc = MusicScanner(db, [str(tmp_path)], mpv_path=mpv_path)
    return sc, db


class TestScannerUnplayable:
    def test_bad_file_not_inserted(self, tmp_path):
        """mpv exit≠0 → 不入库 + unplayable 计数 +1。"""
        sc, db = _mk(tmp_path)
        p = tmp_path / "bad.mp3"
        p.write_bytes(b"xx")
        with patch("app.music.scanner.subprocess.Popen") as P:
            P.return_value.wait.return_value = 2  # mpv: Failed to recognize
            sc._process_file(str(p), False)
        assert db.get_song_by_path(str(p)) is None
        assert sc._unplayable_count == 1

    def test_good_file_inserted(self, tmp_path):
        """mpv exit=0 → 正常入库（元数据走文件名兜底）。"""
        sc, db = _mk(tmp_path)
        p = tmp_path / "good.mp3"
        p.write_bytes(b"xx")
        with patch("app.music.scanner.subprocess.Popen") as P:
            P.return_value.wait.return_value = 0
            sc._process_file(str(p), False)
        row = db.get_song_by_path(str(p))
        assert row is not None
        assert row["title"]

    def test_mpv_unavailable_degrade(self, tmp_path):
        """mpv 路径不存在 → _verify_mpv_ok False，跳过验证、照常入库。"""
        sc, db = _mk(tmp_path, mpv_path=str(tmp_path / "no_such_mpv.exe"))
        assert sc._verify_mpv_ok is False
        p = tmp_path / "g.mp3"
        p.write_bytes(b"xx")
        with patch("app.music.scanner.subprocess.Popen") as P:
            sc._process_file(str(p), False)
            P.assert_not_called()
        assert db.get_song_by_path(str(p)) is not None

    def test_timeout_kills_and_skips(self, tmp_path):
        """mpv 卡住（TimeoutExpired）→ 杀进程、判不可播、不入库、不卡扫描。"""
        sc, db = _mk(tmp_path)
        p = tmp_path / "hang.mp3"
        p.write_bytes(b"xx")
        with patch("app.music.scanner.subprocess.Popen") as P:
            proc = P.return_value
            proc.wait.side_effect = TimeoutExpired(cmd="mpv", timeout=sc.VERIFY_TIMEOUT)
            t0 = time.time()
            sc._process_file(str(p), False)
            dt = time.time() - t0
        assert db.get_song_by_path(str(p)) is None
        proc.kill.assert_called()
        assert dt < 1.5, f"扫描不应被 mpv 卡住，实际 {dt:.2f}s"
        db.close()
