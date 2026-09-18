"""
生成测试 WAV 文件
"""
import asyncio
import edge_tts
import os

OUTPUT_DIR = r"C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent\tools\asr_fixtures"
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_CASES = [
    ("short_next", "下一首"),
    ("music_query", "播放一首比较安静的音乐"),
    ("lol_mid_missing", "对面中单消失了"),
    ("lol_teamfight", "我还有闪现，这波可以打团"),
]

async def generate():
    for name, text in TEST_CASES:
        out_path = os.path.join(OUTPUT_DIR, f"{name}.mp3")
        wav_path = os.path.join(OUTPUT_DIR, f"{name}.wav")
        
        communicate = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural")
        await communicate.save(out_path)
        print(f"生成: {name}.mp3")
        
        # 用 ffmpeg 转成 wav 16kHz mono
        # 先检查 ffmpeg
        import subprocess
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", out_path, "-ar", "16000", "-ac", "1", wav_path],
                capture_output=True, timeout=30
            )
            print(f"转换: {name}.wav (16kHz mono)")
        except FileNotFoundError:
            # ffmpeg 不存在，直接用 mp3
            print(f"ffmpeg 不存在，保留 mp3: {name}.mp3")

asyncio.run(generate())
print("完成")
