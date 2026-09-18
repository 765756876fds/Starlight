"""
Qwen3-ASR 真人麦克风测试脚本
交互模式：按 Enter 开始录音，说完再按 Enter 结束
"""
import pyaudio
import wave
import time
import requests
import os
import sys
import threading
import msvcrt
import re
import json

# 配置
ASR_URL = "http://127.0.0.1:8080/v1/audio/transcriptions"
RATE = 16000
CHANNELS = 1
CHUNK = 1024
RECORD_DIR = r"C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent\tools\asr_records"
os.makedirs(RECORD_DIR, exist_ok=True)

# 测试语料
TEST_CASES = [
    # 普通中文 (5)
    ("普通1", "你好"),
    ("普通2", "今天天气不错"),
    ("普通3", "我准备开始玩游戏了"),
    ("普通4", "等一下我马上回来"),
    ("普通5", "这首歌还挺好听"),
    
    # 音乐控制 (10)
    ("音乐1", "下一首"),
    ("音乐2", "上一首"),
    ("音乐3", "暂停音乐"),
    ("音乐4", "播放音乐"),
    ("音乐5", "继续播放"),
    ("音乐6", "换一首"),
    ("音乐7", "放首安静点的"),
    ("音乐8", "声音小一点"),
    ("音乐9", "声音大一点"),
    ("音乐10", "播放一首比较安静的音乐"),
    
    # LoL 术语 (10)
    ("LoL1", "对面中单消失了"),
    ("LoL2", "对面打野可能在上路"),
    ("LoL3", "我还有闪现"),
    ("LoL4", "这波可以打团"),
    ("LoL5", "我没闪现了"),
    ("LoL6", "对面辅助不见了"),
    ("LoL7", "小龙还有一分钟"),
    ("LoL8", "大龙刷新了"),
    ("LoL9", "我们可以推高地"),
    ("LoL10", "对面中单回城了"),
    
    # 英雄名 (5)
    ("英雄1", "亚索"),
    ("英雄2", "伊泽瑞尔"),
    ("英雄3", "嘉文四世"),
    ("英雄4", "皮城女警"),
    ("英雄5", "诺克萨斯之手"),
]

def record_audio(filename):
    """录音，按 Enter 开始和结束"""
    p = pyaudio.PyAudio()
    
    stream = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK
    )
    
    print("  录音中... (按 Enter 结束)")
    frames = []
    stop_flag = False
    
    def wait_for_enter():
        msvcrt.getch()
        nonlocal stop_flag
        stop_flag = True
    
    t = threading.Thread(target=wait_for_enter)
    t.start()
    
    while not stop_flag:
        data = stream.read(CHUNK)
        frames.append(data)
    
    stream.stop_stream()
    stream.close()
    p.terminate()
    
    # 保存 WAV
    wf = wave.open(filename, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(2)
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()
    
    duration = len(frames) * CHUNK / RATE
    print(f"  录音完成: {duration:.2f}s")
    return filename, duration

def transcribe(filename):
    """调用 ASR API 转录，返回 (text, latency_ms)"""
    with open(filename, 'rb') as f:
        files = {'file': ('audio.wav', f, 'audio/wav')}
        data = {'model': 'default'}
        
        t0 = time.time()
        resp = requests.post(ASR_URL, files=files, data=data, timeout=60)
        t1 = time.time()
    
    if resp.status_code == 200:
        result = resp.json()
        text = result.get('text', '')
        # 提取 <asr_text> 内容
        match = re.search(r'<asr_text>(.*?)(?:</asr_text>|$)', text)
        if match:
            text = match.group(1).strip()
        return text, (t1 - t0) * 1000
    else:
        return f"ERROR: HTTP {resp.status_code}", (t1 - t0) * 1000

def main():
    print("=" * 60)
    print("Qwen3-ASR 真人麦克风测试")
    print("=" * 60)
    print()
    print("说明：")
    print("  - 每句话测试 3 次")
    print("  - 按 Enter 开始录音，说完再按 Enter 结束")
    print("  - 保持正常说话音量和速度")
    print()
    
    results = []
    
    for idx, (name, expected) in enumerate(TEST_CASES, 1):
        print(f"[{idx}/{len(TEST_CASES)}] {name}: 请说「{expected}」")
        
        runs = []
        for run in range(3):
            print(f"  第 {run+1} 次:")
            input("    按 Enter 开始录音...")
            
            filename = os.path.join(RECORD_DIR, f"{name}_run{run+1}.wav")
            record_audio(filename)
            
            text, latency_ms = transcribe(filename)
            print(f"    识别: {text}")
            print(f"    延迟: {latency_ms:.0f}ms")
            runs.append({"text": text, "latency_ms": latency_ms})
        
        results.append({
            "name": name,
            "expected": expected,
            "runs": runs,
        })
        
        print()
    
    # 汇总
    print("=" * 60)
    print("汇总")
    print("=" * 60)
    
    for r in results:
        print(f"\n{r['name']} (预期: {r['expected']})")
        for i, run in enumerate(r['runs'], 1):
            print(f"  Run {i}: {run['text']} | {run['latency_ms']:.0f}ms")
    
    # 统计
    all_latencies = []
    for r in results:
        for run in r['runs']:
            all_latencies.append(run['latency_ms'])
    
    all_latencies.sort()
    n = len(all_latencies)
    
    print(f"\n{'='*60}")
    print(f"统计")
    print(f"{'='*60}")
    print(f"总测试次数: {n}")
    print(f"P50: {all_latencies[n//2]:.0f}ms")
    print(f"P95: {all_latencies[int(n*0.95)]:.0f}ms")
    print(f"最大: {all_latencies[-1]:.0f}ms")
    
    # 保存结果
    result_file = os.path.join(RECORD_DIR, "test_results.json")
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到: {result_file}")

if __name__ == '__main__':
    main()
