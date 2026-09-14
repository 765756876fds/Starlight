"""
测试脚本：音乐模块基本功能
测试数据库、扫描器、搜索
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.music.database import MusicDatabase
from app.music.scanner import MusicScanner
from app.music.search import MusicSearch


def test_database():
    """测试数据库"""
    print("=" * 50)
    print("测试 1: 数据库创建")
    print("=" * 50)

    db = MusicDatabase("./data/test_music.db")
    print(f"数据库路径: {db.db_path}")
    print(f"歌曲数量: {db.get_song_count()}")
    print("数据库创建成功！")
    return db


def test_scanner(db):
    """测试扫描器"""
    print("\n" + "=" * 50)
    print("测试 2: 音乐扫描")
    print("=" * 50)

    scan_dirs = ["D:\\落雪\\音乐文件"]
    extensions = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".ape"}

    scanner = MusicScanner(db, scan_dirs, extensions)
    result = scanner.scan(full_rescan=True)

    print(f"扫描结果: {result}")
    print(f"数据库歌曲数: {db.get_song_count()}")

    # 显示前 10 首
    songs = db.get_all_songs(limit=10)
    print(f"\n前 10 首歌曲:")
    for i, song in enumerate(songs, 1):
        duration = song.get('duration') or 0
        print(f"  {i}. {song.get('artist') or '未知'} - {song.get('title') or '未知'} "
              f"({duration:.0f}s)")

    return result


def test_search(db):
    """测试搜索"""
    print("\n" + "=" * 50)
    print("测试 3: 音乐搜索")
    print("=" * 50)

    search = MusicSearch(db)

    # 测试搜索
    test_queries = ["周杰伦", "勇敢", "动漫", "Butter-Fly"]
    for query in test_queries:
        results = search.search(query, limit=5)
        print(f"\n搜索 '{query}': {len(results)} 个结果")
        for i, song in enumerate(results[:3], 1):
            score = song.get("search_score", 0)
            print(f"  {i}. [{score:.2f}] {song.get('artist', '?')} - {song.get('title', '?')}")


def test_artists_albums(db):
    """测试歌手和专辑"""
    print("\n" + "=" * 50)
    print("测试 4: 歌手和专辑")
    print("=" * 50)

    artists = db.get_artists()
    print(f"\n歌手列表 (前 10):")
    for i, artist in enumerate(artists[:10], 1):
        print(f"  {i}. {artist['artist']} - {artist['song_count']} 首歌, "
              f"{artist['album_count']} 张专辑")

    albums = db.get_albums()
    print(f"\n专辑数量: {len(albums)}")


if __name__ == "__main__":
    print("LoL Music Agent - 音乐模块测试")
    print()

    try:
        db = test_database()
        test_scanner(db)
        test_search(db)
        test_artists_albums(db)

        print("\n" + "=" * 50)
        print("所有测试完成！")
        print("=" * 50)

    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
