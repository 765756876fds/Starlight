"""审计测试：ASR 和 TTS 检查"""
import sys, os
sys.path.insert(0, '.')

print('=== C7. ASR 检查 ===')

# 检查 ASR 配置
from app.core.config import get_config
config = get_config()
asr_config = config.get_section('asr')
print(f'默认引擎: {asr_config.get("engine")}')
print(f'Qwen3-ASR 模型路径: {asr_config.get("qwen3_asr_model")}')
print(f'Qwen3-ASR 模型存在: {os.path.exists(asr_config.get("qwen3_asr_model", ""))}')
print(f'Qwen3-ASR mmproj 路径: {asr_config.get("qwen3_asr_mmproj")}')
print(f'Qwen3-ASR mmproj 存在: {os.path.exists(asr_config.get("qwen3_asr_mmproj", ""))}')
print(f'Faster-Whisper 配置: {asr_config.get("faster_whisper")}')

# 检查 faster-whisper 是否可用
print('\n--- Faster-Whisper 可用性 ---')
try:
    from faster_whisper import WhisperModel
    print('faster-whisper: 已安装 ✅')
except ImportError as e:
    print(f'faster-whisper: 未安装 ❌ ({e})')

# 检查 llama-cpp-python 是否可用
print('\n--- llama-cpp-python 可用性 ---')
try:
    import llama_cpp
    print(f'llama-cpp-python: 已安装 (版本 {llama_cpp.__version__}) ✅')
except ImportError:
    print('llama-cpp-python: 未安装 ❌ (需要 C++ 编译器)')

# 检查 ASR 管理器初始化
print('\n--- ASRManager 初始化测试 ---')
try:
    from app.voice.asr import ASRManager
    asr = ASRManager(asr_config)
    # 不实际加载模型（需要时间和资源），只检查初始化
    print(f'ASRManager 创建成功 ✅')
    print(f'引擎列表: {len(asr.engines)} 个')
except Exception as e:
    print(f'ASRManager 初始化失败 ❌: {e}')

# 检查 pyaudio
print('\n--- PyAudio 可用性 ---')
try:
    import pyaudio
    p = pyaudio.PyAudio()
    print(f'PyAudio: 已安装 ✅')
    print(f'输入设备数: {p.get_device_count()}')
    p.terminate()
except ImportError:
    print('PyAudio: 未安装 ❌')
except Exception as e:
    print(f'PyAudio: 初始化异常 ❌: {e}')

print('\n=== C8. TTS 检查 ===')

# 检查 Edge TTS
print('\n--- Edge TTS 可用性 ---')
try:
    import edge_tts
    print('edge-tts: 已安装 ✅')
except ImportError:
    print('edge-tts: 未安装 ❌')

# 检查 TTS 配置
tts_config = config.get_section('tts')
print(f'\nTTS 引擎: {tts_config.get("engine")}')
print(f'Edge TTS 语音: {tts_config.get("edge_voice")}')
print(f'输出目录: {tts_config.get("output_dir")}')

# 实际测试 Edge TTS 生成
print('\n--- Edge TTS 实际生成测试 ---')
try:
    import asyncio
    import edge_tts

    async def test_tts():
        output_file = './data/tts_test.mp3'
        communicate = edge_tts.Communicate(
            text="这是一个语音合成测试，确认 Edge TTS 可以正常工作。",
            voice="zh-CN-XiaoxiaoNeural",
        )
        await communicate.save(output_file)
        return output_file

    output = asyncio.run(test_tts())
    file_size = os.path.getsize(output) if os.path.exists(output) else 0
    print(f'TTS 生成成功 ✅')
    print(f'输出文件: {output}')
    print(f'文件大小: {file_size} 字节')
except Exception as e:
    print(f'TTS 生成失败 ❌: {e}')
    import traceback
    traceback.print_exc()

print('\n=== ASR/TTS 检查完成 ===')
