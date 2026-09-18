"""
验证 Ollama 音频转录端点
先用 Edge TTS 生成测试音频，再调用转录 API
"""
import asyncio
import edge_tts
import requests
import time
import os

OLLAMA_URL = "http://localhost:11434/v1/audio/transcriptions"
MODEL = "qwen3-asr-0.6b"

TEST_TEXT = "下一首，暂停音乐，音量调大一点"
WAV_FILE = "_tts_test.wav"

async def generate_tts():
    """用 Edge TTS 生成测试音频"""
    communicate = edge_tts.Communicate(TEST_TEXT, "zh-CN-XiaoxiaoNeural")
    await communicate.save(WAV_FILE)
    print(f"TTS 生成完成: {WAV_FILE}")

def transcribe(filename):
    """调用 Ollama 转录"""
    with open(filename, 'rb') as f:
        files = {'file': (filename, f, 'audio/wav')}
        data = {'model': MODEL}
        
        t0 = time.time()
        response = requests.post(OLLAMA_URL, files=files, data=data, timeout=60)
        t1 = time.time()
    
    print(f"HTTP 状态码: {response.status_code}")
    print(f"耗时: {t1-t0:.2f}s")
    
    if response.status_code == 200:
        result = response.json()
        print(f"转录结果: {result.get('text', '')}")
        return True
    else:
        print(f"错误: {response.text[:500]}")
        return False

if __name__ == '__main__':
    print("=== Step 1: 生成 TTS 测试音频 ===")
    asyncio.run(generate_tts())
    
    print()
    print("=== Step 2: Ollama 音频转录测试 ===")
    print(f"测试文本: {TEST_TEXT}")
    success = transcribe(WAV_FILE)
    
    # 清理
    if os.path.exists(WAV_FILE):
        os.remove(WAV_FILE)
    
    print()
    if success:
        print("✅ Ollama 音频转录端点可用！")
    else:
        print("❌ Ollama 音频转录端点不可用")
