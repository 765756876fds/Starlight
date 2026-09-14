"""审计测试：SQLite 音乐库"""
import sqlite3, os, sys
sys.path.insert(0, '.')

db_path = './data/test_music.db'
print('=== SQLite 音乐库审计 ===')
print(f'数据库路径: {os.path.abspath(db_path)}')
print(f'文件存在: {os.path.exists(db_path)}')
print(f'文件大小: {os.path.getsize(db_path)} 字节')

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print('\n--- 表列表 ---')
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cursor.fetchall()]
for t in tables:
    cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
    cnt = cursor.fetchone()[0]
    print(f'  {t}: {cnt} 行')

print('\n--- music 表结构 ---')
cursor.execute('PRAGMA table_info(music)')
for col in cursor.fetchall():
    print(f'  {col[1]} ({col[2]})')

print('\n--- FTS5 检查 ---')
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'")
fts_tables = [r[0] for r in cursor.fetchall()]
print(f'FTS 表: {fts_tables}')

cursor.execute('SELECT COUNT(*) FROM music')
count = cursor.fetchone()[0]
print(f'\n实际歌曲数量: {count}')

print('\n--- 随机抽查 5 首 ---')
cursor.execute('SELECT id, title, artist, album, duration, path FROM music ORDER BY RANDOM() LIMIT 5')
for row in cursor.fetchall():
    print(f'  [{row[0]}] {row[2]} - {row[1]} | {row[3]} | {row[4]}s')
    print(f'       路径: {row[5]}')
    print(f'       文件存在: {os.path.exists(row[5])}')

print('\n--- 中文搜索测试 (FTS5) ---')
try:
    cursor.execute("SELECT m.title, m.artist FROM music_fts JOIN music m ON m.id=music_fts.rowid WHERE music_fts MATCH '勇敢' LIMIT 3")
    results = cursor.fetchall()
    print(f'搜索"勇敢": {len(results)} 结果')
    for r in results:
        print(f'  {r[1]} - {r[0]}')
except Exception as e:
    print(f'FTS 搜索失败: {e}')

print('\n--- LIKE 搜索测试 ---')
cursor.execute("SELECT title, artist FROM music WHERE title LIKE '%勇敢%' OR artist LIKE '%勇敢%' LIMIT 3")
results = cursor.fetchall()
print(f'搜索"勇敢": {len(results)} 结果')
for r in results:
    print(f'  {r[1]} - {r[0]}')

conn.close()
print('\n=== SQLite 审计完成 ===')
