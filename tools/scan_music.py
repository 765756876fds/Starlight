"""
扫描音乐文件并写入数据库
"""
import sys
sys.path.insert(0, '.')

from app.music.database import MusicDatabase
from app.music.scanner import MusicScanner

DB_PATH = './data/music.db'
SCAN_DIR = r'D:\落雪\音乐文件'

print("=" * 60)
print("扫描音乐文件")
print("=" * 60)
print(f"数据库: {DB_PATH}")
print(f"扫描目录: {SCAN_DIR}")

db = MusicDatabase(DB_PATH)
print(f'\n扫描前: {db.get_song_count()} 首')

scanner = MusicScanner(db, [SCAN_DIR])
result = scanner.scan(full_rescan=True)

print(f'\n扫描完成:')
print(f'  扫描文件: {result.get("scanned", 0)}')
print(f'  新增: {result.get("added", 0)}')
print(f'  更新: {result.get("updated", 0)}')
print(f'  跳过: {result.get("skipped", 0)}')
print(f'  错误: {result.get("errors", 0)}')

print(f'\n扫描后: {db.get_song_count()} 首')

db.close()
print("\n完成！")
