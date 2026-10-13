#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen-TTS 引擎试听：6 模型 × 2 端点（独立脚本，不依赖项目模块）"""
import argparse
import os
import sys
import time

import requests

# 实测：专属域名(ws-xaw018hgkwk5xcvw)对该 Key 返回 403 Endpoint.AccessDenied（Key 不属于该空间）
# 通用域名实测 200 正常合成 -> 用通用域名
BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
MODAL_ENDPOINT = BASE_URL + "/services/aigc/multimodal-generation/generation"
AUDIO_ENDPOINT = BASE_URL + "/services/audio/tts/SpeechSynthesizer"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".qwen_key")

TEXT = "欢迎使用洛尔音乐助手。技能冷却完毕，可以再次释放闪现。敌方打野正在接近，请注意走位。"

# (模型, 端点, 音色列表) —— 仅用户已开通模型；realtime 实测不支持 HTTP 调用，已剔除
MATRIX = [
    ("qwen3-tts-flash",           MODAL_ENDPOINT, ["Cherry", "Serena", "Mia", "Neil"]),
    ("qwen-audio-3.1-tts-flash",  AUDIO_ENDPOINT, ["longanhuan_v3.1", "longanlingxin_v3.1", "yuxiaoyun_v3.1"]),
    ("qwen-audio-3.1-tts-next",   AUDIO_ENDPOINT, ["longanhuan_v3.1", "yuxiaoyun_v3.1"]),  # 100万额度、重点
    ("qwen-audio-3.0-tts-flash",  AUDIO_ENDPOINT, ["longanhuan_v3.6", "longanlingxin"]),
    ("qwen-audio-3.0-tts-plus",   AUDIO_ENDPOINT, ["longanlingxin", "longanlufeng"]),
]

def load_key(arg_key: str) -> str:
    if arg_key:
        return arg_key
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get("DASHSCOPE_API_KEY", "")

def synthesize(api_key: str, model: str, endpoint: str, voice: str, out_path: str) -> float:
    t0 = time.monotonic()
    body = {"model": model, "input": {"voice": voice}}
    # 3.1-tts-next 走 text_prompt（音频生成协议）；其余 TTS 走 text
    if "next" in model:
        body["input"]["text_prompt"] = TEXT
    else:
        body["input"]["text"] = TEXT
    if endpoint == AUDIO_ENDPOINT:
        body["input"].update({"format": "wav", "sample_rate": 24000})
    else:
        body["input"]["language_type"] = "Chinese"
    resp = requests.post(endpoint, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    audio = data.get("output", {}).get("audio", {})
    if not audio.get("url"):
        raise RuntimeError(f"无音频 URL: {data.get('code', resp.text[:200])}")
    url = audio["url"]
    audio_resp = requests.get(url, timeout=120)
    audio_resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(audio_resp.content)
    return time.monotonic() - t0

def main():
    parser = argparse.ArgumentParser(description="Qwen-TTS 6 模型试听")
    parser.add_argument("--key", default="", help="API Key（默认读 tools/.qwen_key）")
    args = parser.parse_args()
    api_key = load_key(args.key)
    if not api_key:
        print("[错误] 未找到 API Key：检查 tools/.qwen_key 或用 --key sk-xxx")
        sys.exit(1)
    out_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tts_qwen_test")
    os.makedirs(out_root, exist_ok=True)
    print(f"输出目录: {out_root}\n")
    ok, fail = 0, 0
    for model, endpoint, voices in MATRIX:
        mdir = os.path.join(out_root, model)
        os.makedirs(mdir, exist_ok=True)
        for voice in voices:
            out = os.path.join(mdir, f"{voice}.wav")
            try:
                secs = synthesize(api_key, model, endpoint, voice, out)
                size = os.path.getsize(out) / 1024
                ok += 1
                print(f"[✓] {model:28s} {voice:20s} 耗时 {secs:.1f}s  {size:.0f}KB  {out}")
            except Exception as e:
                fail += 1
                print(f"[✗] {model:28s} {voice:20s} 失败: {e}")
    print(f"\n完成: 成功 {ok} / 失败 {fail} / 共 {ok + fail}。请打开 data/tts_qwen_test/ 试听。")

if __name__ == "__main__":
    main()
