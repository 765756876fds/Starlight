"""
测试 llama-server 常驻音频转录 API
"""
import requests
import time
import os

URL = "http://127.0.0.1:8080/v1/audio/transcriptions"
AUDIO_DIR = r"C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent\tools\asr_fixtures"

TEST_FILES = [
    ("short_next.mp3", "下一首"),
    ("music_query.mp3", "播放一首比较安静的音乐"),
    ("lol_mid_missing.mp3", "对面中单消失了"),
    ("lol_teamfight.mp3", "我还有闪现，这波可以打团"),
]

print("=== llama-server 常驻音频转录测试 ===")
print()

# 第一次测试（warm up）
print("--- Warm up ---")
audio_path = os.path.join(AUDIO_DIR, "short_next.mp3")
with open(audio_path, 'rb') as f:
    files = {'file': ('test.mp3', f, 'audio/mpeg')}
    data = {'model': 'default'}
    t0 = time.time()
    resp = requests.post(URL, files=files, data=data, timeout=60)
    t1 = time.time()
print(f"  HTTP {resp.status_code}")
print(f"  结果: {resp.text[:200]}")
print(f"  耗时: {t1-t0:.2f}s")
print()

# 10 次延迟测试
print("--- 10 次延迟测试 (short_next) ---")
latencies = []
for i in range(10):
    with open(audio_path, 'rb') as f:
        files = {'file': ('test.mp3', f, 'audio/mpeg')}
        data = {'model': 'default'}
        t0 = time.time()
        resp = requests.post(URL, files=files, data=data, timeout=60)
        t1 = time.time()
    
    latency = t1 - t0
    latencies.append(latency)
    
    if resp.status_code == 200:
        result = resp.json().get('text', '')
        print(f"  Run {i+1}: {result[:30]} | {latency*1000:.0f}ms")
    else:
        print(f"  Run {i+1}: HTTP {resp.status_code} | {latency*1000:.0f}ms")

print()
print("--- 统计 ---")
print(f"  最快: {min(latencies)*1000:.0f}ms")
print(f"  最慢: {max(latencies)*1000:.0f}ms")
print(f"  平均: {sum(latencies)/len(latencies)*1000:.0f}ms")
print(f"  中位数: {sorted(latencies)[len(latencies)//2]*1000:.0f}ms")

# 测试所有 4 个文件
print()
print("--- 所有测试文件 ---")
for audio_file, expected in TEST_FILES:
    audio_path = os.path.join(AUDIO_DIR, audio_file)
    with open(audio_path, 'rb') as f:
        files = {'file': (audio_file, f, 'audio/mpeg')}
        data = {'model': 'default'}
        t0 = time.time()
        resp = requests.post(URL, files=files, data=data, timeout=60)
        t1 = time.time()
    
    if resp.status_code == 200:
        result = resp.json().get('text', '')
        print(f"  {audio_file}: {result[:40]} | {(t1-t0)*1000:.0f}ms")
    else:
        print(f"  {audio_file}: HTTP {resp.status_code}")
