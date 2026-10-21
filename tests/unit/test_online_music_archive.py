"""ArchiveSource + custom loader 测试（mock HTTP，不真联网）。"""
import pytest

pytestmark = pytest.mark.unit

import os
import textwrap

from app.music.online.models import OnlineTrack
from app.music.online.manager import OnlineSourceManager
from app.music.online.source import MusicSource


class _FakeArchive(OnlineSourceManager if False else object):
    pass


def test_archive_search_parses(monkeypatch):
    from app.music.online.sources.archive_source import ArchiveSource
    payload = {
        "response": {"docs": [
            {"identifier": "demo1", "title": "Song A", "creator": "Artist A"},
            {"identifier": "demo2", "title": ["Song B"], "creator": ["Artist B"]},
        ]}
    }
    def fake_get(self, url):
        return payload
    monkeypatch.setattr("app.music.online.sources.archive_source.ArchiveSource._http_get_json", fake_get)
    src = ArchiveSource()
    out = src.search("foo", limit=10)
    assert len(out) == 2
    assert out[0].track_id == "demo1"
    assert out[0].artist == "Artist A"
    assert out[1].artist == "Artist B"


def test_archive_get_play_url(monkeypatch):
    from app.music.online.sources.archive_source import ArchiveSource
    meta = {"files": [
        {"name": "cover.jpg", "format": "JPEG"},
        {"name": "track1_mp3.mp3", "format": "VBR MP3"},
    ]}
    monkeypatch.setattr(
        "app.music.online.sources.archive_source.ArchiveSource._http_get_json",
        lambda self, url: meta,
    )
    src = ArchiveSource()
    t = OnlineTrack(source_name="archive", track_id="demo", title="x",
                    extra={"identifier": "demo"})
    url = src.get_play_url(t)
    assert url and "track1_mp3.mp3" in url


def test_load_custom_sources(tmp_path):
    # 合法文件
    (tmp_path / "good.py").write_text(textwrap.dedent('''
        from app.music.online.source import MusicSource
        from app.music.online.models import OnlineTrack
        class S(MusicSource):
            name = "good"
            display_name = "Good"
            def search(self, k, limit=20): return []
            def get_play_url(self, t): return None
            def get_lyric(self, t): return None
        def create_source(): return S()
    '''), encoding="utf-8")
    # 非法文件（无 create_source）
    (tmp_path / "bad.py").write_text("x = 1\n", encoding="utf-8")
    # 模板（.template 不会被加载）
    (tmp_path / "skip.template").write_text("ignored", encoding="utf-8")

    m = OnlineSourceManager()
    n = m.load_custom_sources(str(tmp_path))
    assert n == 1
    assert "good" in m.list_sources()
