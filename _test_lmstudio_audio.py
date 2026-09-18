"""
测试 LM Studio API 是否支持音频输入
"""
from openai import OpenAI
import os

client = OpenAI(base_url='http://127.0.0.1:1234/v1', api_key='lm-studio')

print('=== 测试 1: audio transcriptions 端点 ===')
try:
    # 尝试调用 transcriptions（需要音频文件）
    # 先创建一个简短的静音 WAV 测试
    import wave
    import struct
    
    # 创建 1 秒静音 WAV
    test_wav = '_test_silence.wav'
    with wave.open(test_wav, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        for _ in range(16000):
            wf.writeframes(struct.pack('<h', 0))
    
    with open(test_wav, 'rb') as f:
        result = client.audio.transcriptions.create(
            model='qwen3-asr-0.6b',
            file=f,
        )
    print(f'  结果: {result}')
    os.remove(test_wav)
except Exception as e:
    print(f'  错误: {type(e).__name__}: {e}')
    if os.path.exists('_test_silence.wav'):
        os.remove('_test_silence.wav')

print()
print('=== 测试 2: chat completions 带音频 ===')
try:
    import base64
    
    # 创建测试音频
    test_wav = '_test_silence.wav'
    with wave.open(test_wav, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        for _ in range(16000):
            wf.writeframes(struct.pack('<h', 0))
    
    with open(test_wav, 'rb') as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    
    os.remove(test_wav)
    
    # 尝试 chat completions 传音频
    response = client.chat.completions.create(
        model='qwen3-asr-0.6b',
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请转录这段音频"},
                    {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}
                ]
            }
        ],
    )
    print(f'  结果: {response.choices[0].message.content}')
except Exception as e:
    print(f'  错误: {type(e).__name__}: {e}')

print()
print('=== 测试 3: 直接 chat 测试 ASR 模型 ===')
try:
    response = client.chat.completions.create(
        model='qwen3-asr-0.6b',
        messages=[
            {"role": "user", "content": "你好，请介绍一下你自己"}
        ],
        max_tokens=100,
    )
    print(f'  结果: {response.choices[0].message.content[:200]}')
except Exception as e:
    print(f'  错误: {type(e).__name__}: {e}')
