"""
Qwen3-ASR Ollama 音频转录测试
"""
import pyaudio
import wave
import time
import requests
import os

# 配置
OLLAMA_URL = "http://localhost:11434/v1/audio/transcriptions"
MODEL = "qwen3-asr-0.6b"
RATE = 16000
CHANNELS = 1
CHUNK = 1024
RECORD_SECONDS = 3

def list_microphones():
    """列出麦克风设备"""
    p = pyaudio.PyAudio()
    print("=== 可用输入设备 ===")
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info['maxInputChannels'] > 0:
            print(f"  [{i}] {info['name']} ({info['maxInputChannels']}ch, {int(info['defaultSampleRate'])}Hz)")
    p.terminate()

def record_audio(filename, seconds=RECORD_SECONDS):
    """录制音频"""
    p = pyaudio.PyAudio()
    
    # 使用默认输入设备
    stream = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK
    )
    
    print(f"  录音中... ({seconds}秒)")
    frames = []
    for _ in range(0, int(RATE / CHUNK * seconds)):
        data = stream.read(CHUNK)
        frames.append(data)
    
    stream.stop_stream()
    stream.close()
    p.terminate()
    
    # 保存 WAV
    wf = wave.open(filename, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()
    
    print(f"  已保存: {filename}")
    return filename

def transcribe_audio(filename):
    """调用 Ollama API 转录音频"""
    with open(filename, 'rb') as f:
        files = {
            'file': (filename, f, 'audio/wav'),
        }
        data = {
            'model': MODEL,
        }
        
        t0 = time.time()
        response = requests.post(OLLAMA_URL, files=files, data=data)
        t1 = time.time()
    
    if response.status_code == 200:
        result = response.json()
        text = result.get('text', '')
        print(f"  转录成功: {text}")
        print(f"  耗时: {t1-t0:.2f}s")
        return text, t1 - t0
    else:
        print(f"  错误: HTTP {response.status_code}")
        print(f"  {response.text[:200]}")
        return None, t1 - t0

if __name__ == '__main__':
    list_microphones()
    print()
    
    input("按 Enter 开始录音...")
    
    wav_file = '_test_asr.wav'
    record_audio(wav_file)
    
    print()
    print("=== 转录测试 ===")
    transcribe_audio(wav_file)
    
    # 清理
    if os.path.exists(wav_file):
        os.remove(wav_file)
