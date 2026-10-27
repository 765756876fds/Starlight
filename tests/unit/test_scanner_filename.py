"""扫描器文件名解析方向测试（"歌名 - 歌手"）

覆盖：
- "歌名 - 歌手.mp3" → title=歌名、artist=歌手
- 无 " - " → title=整名、artist 空
- ID3 有值 → 用标签（不被文件名覆盖）
- 标签有 title 无 artist → title 用标签、artist 用文件名后半

注：tools/fix_music_title_artist.py 已在项目大扫除中移除（从未纳入 git），
原 TestFixScript 类随其删除，不再保留对已不存在脚本的测试。
"""
import pytest

from app.music.scanner import MusicScanner

pytestmark = pytest.mark.unit


def _make_scanner() -> MusicScanner:
    return MusicScanner(db=None, scan_dirs=[])


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
