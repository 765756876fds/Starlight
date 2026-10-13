"""Kokoro-82M 本地 TTS 独立测试脚本。

运行: python tools/tts_kokoro_test.py
输出: data/tts_kokoro_test/*.wav（中文 2 个音色 + 英文 1 个对比）
"""
import os
import sys
import time

import soundfile as sf
from kokoro_onnx import Kokoro

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE, "models", "kokoro")
OUT_DIR = os.path.join(BASE, "data", "tts_kokoro_test")
os.makedirs(OUT_DIR, exist_ok=True)

MODEL = os.path.join(MODEL_DIR, "kokoro-v1.0.onnx")
VOICES = os.path.join(MODEL_DIR, "voices-v1.0.bin")

# 贴近项目真实播报场景的测试文本
ZH_TEXT = "欢迎使用洛尔音乐助手。技能冷却完毕，可以再次释放闪现。敌方打野正在接近，请注意走位。"
EN_TEXT = "Welcome to the LoL music assistant. Your flash is ready again."

# Kokoro-82M v1.0 中文音色（zf_* 系列）；espeak-ng 中文语言码为 cmn
CASES = [
    ("zh_female_xiaobei", "zf_xiaobei", "cmn", ZH_TEXT),
    ("zh_female_xiaoni", "zf_xiaoni", "cmn", ZH_TEXT),
    ("en_female_sarah", "af_sarah", "en-us", EN_TEXT),
]


def main() -> int:
    t0 = time.time()
    print(f"加载模型: {MODEL} ...")
    kokoro = Kokoro(MODEL, VOICES)
    print(f"模型加载完成 ({time.time() - t0:.1f}s)")

    for tag, voice, lang, text in CASES:
        t1 = time.time()
        try:
            samples, sr = kokoro.create(text, voice=voice, speed=1.0, lang=lang)
            path = os.path.join(OUT_DIR, f"{tag}.wav")
            sf.write(path, samples, sr)
            dur = len(samples) / sr
            print(f"[OK] {tag:20s} voice={voice:12s} lang={lang:6s} "
                  f"{dur:.1f}s 合成 {time.time() - t1:.1f}s -> {path}")
        except Exception as e:
            print(f"[FAIL] {tag:20s} voice={voice:12s}: {type(e).__name__}: {str(e)[:150]}")

    print(f"\n完成。音频输出目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
