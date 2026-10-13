"""sherpa-onnx 中文 TTS 三模型对比测试脚本。

运行: python tools/tts_sherpa_test.py
输出: data/tts_sherpa_test/*.wav（matcha-zh-baker / fanchen-C / aishell3 同文本）
"""
import os
import sys
import time

import soundfile as sf
import sherpa_onnx

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = os.path.join(BASE, "models", "sherpa")
OUT = os.path.join(BASE, "data", "tts_sherpa_test")
os.makedirs(OUT, exist_ok=True)

TEXT = "欢迎使用洛尔音乐助手。技能冷却完毕，可以再次释放闪现。敌方打野正在接近，请注意走位。"


def build_tts(kind):
    """kind: matcha | fanchen | aishell3"""
    if kind == "matcha":
        d = os.path.join(M, "matcha-icefall-zh-baker")
        model = sherpa_onnx.OfflineTtsModelConfig(
            matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=os.path.join(d, "model-steps-3.onnx"),
                vocoder=os.path.join(M, "vocos-22khz-univ.onnx"),
                lexicon=os.path.join(d, "lexicon.txt"),
                tokens=os.path.join(d, "tokens.txt"),
            ),
            num_threads=2,
        )
        rule = ",".join(os.path.join(d, f) for f in ("phone.fst", "date.fst", "number.fst"))
    elif kind == "fanchen":
        d = os.path.join(M, "vits-zh-hf-fanchen-C")
        model = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=os.path.join(d, "vits-zh-hf-fanchen-C.onnx"),
                lexicon=os.path.join(d, "lexicon.txt"),
                tokens=os.path.join(d, "tokens.txt"),
            ),
            num_threads=2,
        )
        rule = ",".join(
            os.path.join(d, f) for f in ("phone.fst", "date.fst", "number.fst", "new_heteronym.fst")
        )
    elif kind == "aishell3":
        d = os.path.join(M, "vits-zh-aishell3")
        model = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=os.path.join(d, "vits-aishell3.onnx"),
                lexicon=os.path.join(d, "lexicon.txt"),
                tokens=os.path.join(d, "tokens.txt"),
            ),
            num_threads=2,
        )
        rule = ",".join(os.path.join(d, f) for f in ("phone.fst", "date.fst", "number.fst"))
    else:
        raise ValueError(kind)
    cfg = sherpa_onnx.OfflineTtsConfig(model=model, rule_fsts=rule)
    if not cfg.validate():
        raise ValueError(f"{kind}: config invalid")
    return sherpa_onnx.OfflineTts(cfg)


def main() -> int:
    cases = [
        ("matcha-zh-baker", "matcha", 0),
        ("vits-fanchen-C", "fanchen", 0),
        ("vits-aishell3", "aishell3", 0),
    ]
    for tag, kind, sid in cases:
        t0 = time.time()
        try:
            tts = build_tts(kind)
            print(f"[{tag}] 模型加载 {time.time() - t0:.1f}s", flush=True)
            gen = sherpa_onnx.GenerationConfig()
            gen.sid = sid
            gen.speed = 1.0
            t1 = time.time()
            audio = tts.generate(TEXT, gen)
            samples = audio.samples
            sr = audio.sample_rate
            path = os.path.join(OUT, f"{tag}.wav")
            sf.write(path, samples, sr)
            dur = len(samples) / sr
            print(f"[OK] {tag:18s} 音频{dur:.1f}s 合成{time.time() - t1:.1f}s -> {path}", flush=True)
        except Exception as e:
            print(f"[FAIL] {tag:18s} {type(e).__name__}: {str(e)[:200]}", flush=True)
    print(f"\n完成。音频输出目录: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
