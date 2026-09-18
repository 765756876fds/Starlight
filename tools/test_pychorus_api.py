from pychorus import create_chroma, find_chorus
import librosa

print("=== 测试 pychorus API ===")

song_path = r'D:\落雪\音乐文件\K歌之王 - 陈奕迅.flac'

print("create_chroma...")
result = create_chroma(song_path)
print(f"type: {type(result)}")
print(f"len: {len(result) if hasattr(result, '__len__') else 'N/A'}")

if hasattr(result, 'shape'):
    print(f"shape: {result.shape}")
else:
    for i, item in enumerate(result):
        shape = item.shape if hasattr(item, 'shape') else 'N/A'
        print(f"  [{i}] type={type(item).__name__}, shape={shape}")

# 尝试 find_chorus
print("\nfind_chorus...")
total_duration = librosa.get_duration(path=song_path)
sr = 22050  # pychorus 默认

# 看看第一个返回值是什么
if len(result) == 1:
    chroma = result[0]
elif len(result) == 2:
    chroma, sr = result
else:
    chroma = result[0]

print(f"chroma shape: {chroma.shape if hasattr(chroma, 'shape') else 'N/A'}")
print(f"total_duration: {total_duration}")

try:
    result = find_chorus(chroma, sr, total_duration, clip_length=15)
    print(f"find_chorus result type: {type(result)}")
    print(f"find_chorus result: {result}")
except Exception as e:
    print(f"error: {e}")
    import traceback
    traceback.print_exc()
