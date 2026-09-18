"""
Qwen3-ASR + llama.cpp 自动化实验脚本
"""
import subprocess
import time
import os
import re

LLAMA_CLI = r"C:\tools\llama.cpp\llama-mtmd-cli.exe"
MODEL_06B = r"D:\迅雷下载\ggml-org\Qwen3-ASR-0.6B-GGUF\Qwen3-ASR-0.6B-Q8_0.gguf"
MMPROJ_06B = r"D:\迅雷下载\ggml-org\Qwen3-ASR-0.6B-GGUF\mmproj-Qwen3-ASR-0.6B-bf16.gguf"
MODEL_17B = r"D:\迅雷下载\ggml-org\Qwen3-ASR-1.7B-GGUF\Qwen3-ASR-1.7B-Q8_0.gguf"
MMPROJ_17B = r"D:\迅雷下载\ggml-org\Qwen3-ASR-1.7B-GGUF\mmproj-Qwen3-ASR-1.7B-bf16.gguf"

AUDIO_DIR = r"C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent\tools\asr_fixtures"

TEST_FILES = [
    ("short_next.mp3", "下一首"),
    ("music_query.mp3", "播放一首比较安静的音乐"),
    ("lol_mid_missing.mp3", "对面中单消失了"),
    ("lol_teamfight.mp3", "我还有闪现，这波可以打团"),
]

PROMPT = "请转录这段音频的内容"

def run_asr(model, mmproj, audio, n_threads=6):
    """运行 ASR，返回 (识别文本, 耗时)"""
    audio_path = os.path.join(AUDIO_DIR, audio)
    
    cmd = [
        LLAMA_CLI,
        "-m", model,
        "--mmproj", mmproj,
        "--audio", audio_path,
        "-p", PROMPT,
        "-n", "128",
        "-t", str(n_threads),
    ]
    
    t0 = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    t1 = time.time()
    
    output = result.stdout + result.stderr
    
    # 提取 <asr_text> 标签内容
    match = re.search(r'<asr_text>(.*?)(?:</asr_text>|$)', output, re.DOTALL)
    if match:
        text = match.group(1).strip()
    else:
        # 尝试找最后输出
        lines = output.strip().split('\n')
        text = lines[-1] if lines else ""
    
    return text, t1 - t0, output

def test_model(name, model, mmproj, runs=5):
    """测试模型"""
    print(f"\n{'='*60}")
    print(f"测试模型: {name}")
    print(f"{'='*60}")
    
    results = {}
    
    for audio_file, expected in TEST_FILES:
        print(f"\n--- {audio_file} (预期: {expected}) ---")
        runs_list = []
        
        for i in range(runs):
            text, latency, _ = run_asr(model, mmproj, audio_file)
            runs_list.append((text, latency))
            print(f"  Run {i+1}: {text[:50]} | {latency:.2f}s")
        
        results[audio_file] = {
            "expected": expected,
            "runs": runs_list,
        }
    
    return results

if __name__ == '__main__':
    # 先测试 0.6B
    results_06b = test_model("Qwen3-ASR-0.6B (CPU)", MODEL_06B, MMPROJ_06B, runs=3)
    
    # 汇总
    print(f"\n\n{'='*60}")
    print(f"汇总: Qwen3-ASR-0.6B (CPU)")
    print(f"{'='*60}")
    for audio_file, data in results_06b.items():
        print(f"\n{audio_file} (预期: {data['expected']})")
        latencies = [r[1] for r in data['runs']]
        print(f"  平均延迟: {sum(latencies)/len(latencies):.2f}s")
        print(f"  结果: {data['runs'][0][0][:60]}")
