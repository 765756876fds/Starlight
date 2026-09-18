"""
ASR 最小真实测试脚本

测试流程：
1. 加载 faster-whisper 模型
2. 录制 3 段 3 秒音频（用户说三句话）
3. 转录每段音频
4. 测试 Intent Router
"""
import sys, os, time, wave, json
sys.path.insert(0, '.')

import pyaudio
import numpy as np

# 测试配置
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 1024
RECORD_SECONDS = 3
DEVICE_INDEX = 1  # 默认输入设备

OUTPUT_DIR = "./data/asr_test"
os.makedirs(OUTPUT_DIR, exist_ok=True)

print('=' * 60)
print('ASR 最小真实测试')
print('=' * 60)
print()

# ===== 1. 加载模型 =====
print('1. 加载 faster-whisper 模型...')
t0 = time.time()

from faster_whisper import WhisperModel

model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8",
)

t1 = time.time()
load_time = t1 - t0
print(f'   模型加载完成，耗时: {load_time:.1f}s')
print()

# ===== 2. 录音函数 =====
def record_audio(seconds=RECORD_SECONDS, label="test"):
    """录制音频"""
    p = pyaudio.PyAudio()

    stream = p.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        input_device_index=DEVICE_INDEX,
        frames_per_buffer=CHUNK,
    )

    print(f'   开始录制 {seconds} 秒...（请说话）')
    frames = []

    for i in range(0, int(RATE / CHUNK * seconds)):
        data = stream.read(CHUNK)
        frames.append(data)

    print('   录制完成')

    stream.stop_stream()
    stream.close()
    p.terminate()

    # 保存 WAV
    wav_path = os.path.join(OUTPUT_DIR, f"{label}.wav")
    wf = wave.open(wav_path, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(p.get_sample_size(FORMAT))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()

    # 转成 numpy 数组
    audio_np = np.frombuffer(b''.join(frames), dtype=np.int16).astype(np.float32) / 32768.0

    return audio_np, wav_path


def transcribe(audio_np):
    """转录音频"""
    t0 = time.time()
    segments, info = model.transcribe(
        audio_np,
        language="zh",
        beam_size=5,
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments)
    t1 = time.time()
    return text, t1 - t0


# ===== 3. 测试三条命令 =====
test_cases = [
    ("Test 1: 下一首", "下一首"),
    ("Test 2: 暂停音乐", "暂停音乐"),
    ("Test 3: 两分钟提醒我", "两分钟提醒我"),
]

results = []

for label, expected in test_cases:
    print(f'--- {label} ---')
    print(f'   请说: "{expected}"')
    print('   3 秒后开始录音...')
    for i in range(3, 0, -1):
        print(f'   {i}...')
        time.sleep(1)

    audio, wav_path = record_audio(label=label.replace(" ", "_"))

    print('   转录中...')
    text, trans_time = transcribe(audio)

    audio_duration = len(audio) / RATE
    rtf = trans_time / audio_duration if audio_duration > 0 else 0

    print(f'   识别结果: "{text}"')
    print(f'   音频时长: {audio_duration:.2f}s')
    print(f'   转录耗时: {trans_time:.2f}s')
    print(f'   RTF: {rtf:.2f}')
    print()

    results.append({
        "label": label,
        "expected": expected,
        "transcribed": text,
        "audio_duration": audio_duration,
        "transcription_time": trans_time,
        "rtf": rtf,
        "wav_path": wav_path,
    })

# ===== 4. Intent Router 测试 =====
print('=' * 60)
print('ASR -> Intent Router 测试')
print('=' * 60)
print()

from app.voice.intent import RuleBasedIntent

for r in results:
    text = r["transcribed"]
    intent = RuleBasedIntent.match(text)
    if intent:
        intent_str = f'{intent.intent_type} (conf={intent.confidence:.2f})'
    else:
        intent_str = "None (未匹配)"

    print(f'语音: "{r["expected"]}"')
    print(f'  转录: "{text}"')
    print(f'  Intent: {intent_str}')
    print()

# ===== 5. 汇总 =====
print('=' * 60)
print('汇总')
print('=' * 60)
print()
print(f'模型加载时间: {load_time:.1f}s')
print(f'设备: OPPO Enco Air4i (索引 {DEVICE_INDEX})')
print(f'采样率: {RATE} Hz')
print()
print('识别结果:')
for i, r in enumerate(results, 1):
    print(f'  {i}. "{r["expected"]}" -> "{r["transcribed"]}" (转录 {r["transcription_time"]:.2f}s)')

print()
print('测试完成！')
