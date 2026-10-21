"""扫描器文件名解析方向测试（"歌名 - 歌手"）+ 存量修复脚本测试

覆盖：
- "歌名 - 歌手.mp3" → title=歌名、artist=歌手
- 无 " - " → title=整名、artist 空
- ID3 有值 → 用标签（不被文件名覆盖）
- 存量脚本：mock 库 → 修复后 music + play_history 两表都正确
"""
import pytest

pytestmark = pytest.mark.unit

import importlib.util
import os
import sqlite3
import sys

import pytest

from app.music.scanner import MusicScanner

TOOLS_FIX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools", "fix_music_title_artist.py",
)


def _make_scanner() -> MusicScanner:
    return MusicScanner(db=None, scan_dirs=[])


def _load_fix_module():
    spec = importlib.util.spec_from_file_location("fix_mta", TOOLS_FIX)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestParseNameParts:
    """_parse_name_parts / _parse_title_from_filename 纯解析"""

    def test_guitar_song_first(self):
        sc = _make_scanner()
        assert sc._parse_name_parts(r"D:\x\不存在的永远 - 乔洋.mp3") == ["不存在的永远", "乔洋"]
        assert sc._parse_title_from_filename(r"D:\x\不存在的永远 - 乔洋.mp3") == "不存在的永远"

    def test_artist_with_multi_part(self):
        sc = _make_scanner()
        assert sc._parse_name_parts(r"D:\x\All For Love - Tungevaag、Raaban、Richard Smitt.flac") == [
            "All For Love", "Tungevaag、Raaban、Richard Smitt",
        ]

    def test_no_dash(self):
        sc = _make_scanner()
        assert sc._parse_name_parts(r"D:\x\只有歌名.mp3") == ["只有歌名"]
        assert sc._parse_title_from_filename(r"D:\x\只有歌名.mp3") == "只有歌名"

    def test_rsplit_only_last_dash(self):
        """歌名本身含 ' - '：rsplit 只切最后一个分隔符"""
        sc = _make_scanner()
        assert sc._parse_name_parts(r"D:\x\我 - 你 - 歌手.mp3") == ["我 - 你", "歌手"]


class TestExtractMetadata:
    """_extract_metadata 文件名兜底 + ID3 优先"""

    def test_filename_fallback_title_and_artist(self, tmp_path):
        p = tmp_path / "不存在的永远 - 乔洋.mp3"
        p.write_bytes(b"")
        data = _make_scanner()._extract_metadata(str(p))
        assert data["title"] == "不存在的永远"
        assert data["artist"] == "乔洋"

    def test_filename_fallback_no_dash(self, tmp_path):
        p = tmp_path / "只有歌名.mp3"
        p.write_bytes(b"")
        data = _make_scanner()._extract_metadata(str(p))
        assert data["title"] == "只有歌名"
        assert data["artist"] is None

    def test_id3_preferred_over_filename(self, tmp_path, monkeypatch):
        """ID3 有 title+artist → 文件名不覆盖"""
        p = tmp_path / "文件名 - 文件歌手.mp3"
        p.write_bytes(b"")
        sc = _make_scanner()
        monkeypatch.setattr("app.music.scanner.MUTAGEN_AVAILABLE", True)
        fake_tags = type("FakeTags", (), {})()  # truthy
        monkeypatch.setattr(
            "app.music.scanner.MutagenFile",
            lambda fp: type("FA", (), {"info": None, "tags": fake_tags})(),
        )
        monkeypatch.setattr(
            sc, "_parse_tags",
            lambda tags, data, fp: data.update(title="标签标题", artist="标签歌手"),
        )
        data = sc._extract_metadata(str(p))
        assert data["title"] == "标签标题"
        assert data["artist"] == "标签歌手"

    def test_id3_title_only_filename_fills_artist(self, tmp_path, monkeypatch):
        """标签有 title 无 artist → title 用标签、artist 用文件名后半"""
        p = tmp_path / "标签歌 - 文件歌手.mp3"
        p.write_bytes(b"")
        sc = _make_scanner()
        monkeypatch.setattr("app.music.scanner.MUTAGEN_AVAILABLE", True)
        fake_tags = type("FakeTags", (), {})()
        monkeypatch.setattr(
            "app.music.scanner.MutagenFile",
            lambda fp: type("FA", (), {"info": None, "tags": fake_tags})(),
        )
        monkeypatch.setattr(
            sc, "_parse_tags",
            lambda tags, data, fp: data.update(title="标签标题"),
        )
        data = sc._extract_metadata(str(p))
        assert data["title"] == "标签标题"
        assert data["artist"] == "文件歌手"


class TestFixScript:
    """存量修复脚本：mock 库 → music + play_history 两表都正确"""

    def _make_db(self, tmp_path):
        dbp = tmp_path / "m.db"
        conn = sqlite3.connect(str(dbp))
        conn.execute(
            "CREATE TABLE music (id INTEGER PRIMARY KEY, title TEXT, artist TEXT,"
            " filename TEXT, play_count INTEGER DEFAULT 0, last_played REAL)"
        )
        conn.execute(
            "CREATE TABLE play_history (id INTEGER PRIMARY KEY, title TEXT, artist TEXT,"
            " source_type TEXT, music_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO music (id,title,artist,filename,play_count,last_played)"
            " VALUES (26,'乔洋',NULL,'不存在的永远 - 乔洋.mp3',5,123.0)"
        )
        conn.execute(
            "INSERT INTO music (id,title,artist,filename,play_count,last_played)"
            " VALUES (58,'爱乐团王超',NULL,'天涯 - 爱乐团王超.mp3',2,124.0)"
        )
        conn.execute(
            "INSERT INTO music (id,title,artist,filename,play_count,last_played)"
            " VALUES (150,'哦漏、国风堂',NULL,'知我 - 哦漏、国风堂.mp3',3,125.0)"
        )
        conn.execute(
            "INSERT INTO play_history (title,artist,source_type,music_id) VALUES ('乔洋','','local',26)"
        )
        conn.commit()
        return conn

    def test_select_lists_rows_without_writing(self, tmp_path):
        conn = self._make_db(tmp_path)
        mod = _load_fix_module()
        rows = mod.fix(conn, apply=False)
        assert len(rows) == 3
        # 未写库：music 原样
        assert conn.execute("SELECT title,artist FROM music WHERE id=26").fetchone() == ("乔洋", None)
        conn.close()

    def test_apply_fixes_both_tables_and_keeps_stats(self, tmp_path):
        conn = self._make_db(tmp_path)
        mod = _load_fix_module()
        mod.fix(conn, apply=True)
        # music 纠正 + play_count/last_played 保留
        assert conn.execute("SELECT title,artist,play_count,last_played FROM music WHERE id=26").fetchone() == (
            "不存在的永远", "乔洋", 5, 123.0,
        )
        assert conn.execute("SELECT title,artist,play_count,last_played FROM music WHERE id=58").fetchone() == (
            "天涯", "爱乐团王超", 2, 124.0,
        )
        assert conn.execute("SELECT title,artist,play_count,last_played FROM music WHERE id=150").fetchone() == (
            "知我", "哦漏、国风堂", 3, 125.0,
        )
        # play_history 同步纠正（source_type='local' 且 music_id 匹配）
        assert conn.execute(
            "SELECT title,artist FROM play_history WHERE music_id=26"
        ).fetchone() == ("不存在的永远", "乔洋")
        conn.close()
