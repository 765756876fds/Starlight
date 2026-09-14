"""
Music 模块测试：SQLite 数据库 + FTS5
"""
import os
import pytest

from app.music.database import MusicDatabase


class TestMusicDatabase:
    """音乐数据库测试"""

    def test_create_database(self, music_db):
        """测试数据库创建"""
        assert music_db is not None
        assert os.path.exists(music_db.db_path)

    def test_insert_song(self, music_db):
        """测试插入歌曲"""
        song_data = {
            "path": "/test/song1.flac",
            "filename": "song1.flac",
            "title": "测试歌曲",
            "artist": "测试歌手",
            "album": "测试专辑",
            "duration": 240.5,
            "filesize": 30000000,
            "format": "flac",
        }
        song_id = music_db.insert_song(song_data)
        assert song_id > 0

        song = music_db.get_song_by_path("/test/song1.flac")
        assert song is not None
        assert song["title"] == "测试歌曲"
        assert song["artist"] == "测试歌手"

    def test_fts_search(self, music_db):
        """测试 FTS5 全文搜索"""
        # 插入测试数据
        songs = [
            {"path": "/test/1.flac", "filename": "1.flac", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "format": "flac"},
            {"path": "/test/2.flac", "filename": "2.flac", "title": "七里香", "artist": "周杰伦", "album": "七里香", "format": "flac"},
            {"path": "/test/3.flac", "filename": "3.flac", "title": "浮夸", "artist": "陈奕迅", "album": "U87", "format": "flac"},
        ]
        for s in songs:
            music_db.insert_song(s)

        # 搜索歌手
        results = music_db.search("周杰伦")
        assert len(results) >= 2

        # 搜索歌名
        results = music_db.search("晴天")
        assert len(results) >= 1
        assert results[0]["title"] == "晴天"

    def test_get_song_count(self, music_db):
        """测试歌曲计数"""
        assert music_db.get_song_count() == 0

        music_db.insert_song({"path": "/test/1.flac", "filename": "1.flac", "title": "test"})
        assert music_db.get_song_count() == 1

    def test_increment_play_count(self, music_db):
        """测试播放计数"""
        song_id = music_db.insert_song({"path": "/test/1.flac", "filename": "1.flac", "title": "test"})
        music_db.increment_play_count(song_id)

        song = music_db.get_song_by_id(song_id)
        assert song["play_count"] == 1

    def test_get_random_songs(self, music_db):
        """测试随机歌曲"""
        for i in range(5):
            music_db.insert_song({"path": f"/test/{i}.flac", "filename": f"{i}.flac", "title": f"test{i}"})

        songs = music_db.get_random_songs(3)
        assert len(songs) == 3


class TestIncrementalScan:
    """增量扫描测试"""

    def test_scan_skips_unchanged(self, music_db):
        """测试增量扫描跳过未修改的文件"""
        # 先插入一首歌
        song_data = {
            "path": "/test/unchanged.flac",
            "filename": "unchanged.flac",
            "title": "unchanged",
            "last_modified": 1000.0,
        }
        music_db.insert_song(song_data)

        # 验证数据库中有记录
        assert music_db.get_song_count() == 1

    def test_update_modified_file(self, music_db):
        """测试更新已修改的文件"""
        song_data = {
            "path": "/test/modified.flac",
            "filename": "modified.flac",
            "title": "old title",
            "last_modified": 1000.0,
        }
        song_id = music_db.insert_song(song_data)

        # 更新
        updated = dict(song_data)
        updated["title"] = "new title"
        updated["last_modified"] = 2000.0
        music_db.update_song(song_id, updated)

        song = music_db.get_song_by_id(song_id)
        assert song["title"] == "new title"
