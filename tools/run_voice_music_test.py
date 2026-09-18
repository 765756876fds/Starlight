"""
语音 → 音乐控制 最小闭环测试

链路：
    PTT 麦克风 → Qwen3-ASR (llama-server HTTP)
    → Intent Router (Rule First → Qwen3-4B fallback)
    → CommandBus → MusicController → mpv

用法：
    python tools/run_voice_music_test.py

操作：
    按住空格键说话，松开自动识别并执行
    输入 q 退出
"""
import os
import sys
import time
import threading

# 添加项目根目录到 path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from app.voice.asr import Qwen3ServerASR
from app.voice.intent import IntentRouter
from app.voice.ptt import PTTMicrophone
from app.music.controller import MusicController
from app.core.command_bus import get_command_bus, Command


def main():
    print("=" * 60)
    print("语音 → 音乐控制 最小闭环测试")
    print("=" * 60)
    print()

    # ===== 1. 初始化 ASR =====
    print("[1/4] 初始化 ASR (Qwen3-ASR llama-server)...")
    asr = Qwen3ServerASR(base_url="http://127.0.0.1:8080/v1")
    asr.load()
    if not asr.is_available():
        print("❌ ASR 不可用，请先启动 llama-server (port 8080)")
        return
    print()

    # ===== 2. 初始化 Intent Router =====
    print("[2/4] 初始化 Intent Router...")
    intent_config = {
        "rule_based_first": True,
        "openai_api": {
            "enabled": True,
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "qwen3/qwen3-4b-k_m",
            "api_key": "lm-studio",
            "enable_no_think": True,
        },
    }
    intent_router = IntentRouter(intent_config)
    intent_router.initialize()
    print()

    # ===== 3. 初始化 Music Controller =====
    print("[3/4] 初始化 Music Controller...")
    music_config = {
        "database": os.path.join(project_root, "data", "test_music.db"),
        "scan_dirs": [r"D:\落雪\音乐文件"],
        "mpv_path": r"D:\新建文件夹 (2)\mpv.exe",
        "mpv_ipc_pipe": r"\\.\pipe\lol_music_mpv",
        "default_volume": 60,
    }
    music = MusicController(music_config)
    music.start()
    print()

    # ===== 4. 初始化 PTT =====
    print("[4/4] 初始化 PTT 麦克风...")
    print()

    # 统计
    stats = {
        "total": 0,
        "asr_exact": 0,
        "intent_ok": 0,
        "latencies": [],
    }

    def on_recording_start():
        print("\n[PTT] 录音中... (松开空格结束)")

    def on_recording_stop(audio_data: bytes):
        """录音结束后的处理链路"""
        if len(audio_data) < 16000 * 0.3:  # 太短，忽略
            print("[PTT] 录音太短，忽略")
            return

        stats["total"] += 1

        # ASR
        t0 = time.time()
        text = asr.transcribe(audio_data)
        t_asr = time.time() - t0

        if not text:
            print(f"[ASR] 空结果 | {t_asr*1000:.0f}ms")
            return

        # 清理文本（去掉标点）
        clean_text = text.strip().rstrip("。，！？")
        print(f"[ASR] {text} | {t_asr*1000:.0f}ms")

        # Intent
        t1 = time.time()
        intent = intent_router.parse(clean_text)
        t_intent = time.time() - t1

        print(f"[Intent] {intent.intent_type} (conf={intent.confidence:.2f}) | {t_intent*1000:.0f}ms")

        # CommandBus
        if intent.intent_type.startswith("music."):
            t2 = time.time()
            cmd = Command(
                command_type=intent.intent_type,
                params=intent.params,
                source="voice",
            )
            try:
                get_command_bus().execute(cmd)
                t_cmd = time.time() - t2
                print(f"[CommandBus] {intent.intent_type} → 执行成功 | {t_cmd*1000:.0f}ms")

                # 统计
                e2e = time.time() - t0
                stats["latencies"].append(e2e * 1000)
                stats["intent_ok"] += 1

            except Exception as e:
                print(f"[CommandBus] 执行失败: {e}")
        else:
            print(f"[跳过] 非音乐命令: {intent.intent_type}")

        print()
        print(f"[PTT] 就绪 (已完成 {stats['total']} 次)")

    ptt = PTTMicrophone(
        on_recording_start=on_recording_start,
        on_recording_stop=on_recording_stop,
    )
    ptt.start()

    print("=" * 60)
    print("操作说明：")
    print("  - 按住【空格键】开始说话")
    print("  - 松开空格键自动识别并执行")
    print("  - 输入 q 然后回车 退出")
    print("=" * 60)
    print()
    print("[PTT] 就绪")

    # 主线程等待退出
    try:
        while True:
            cmd = input().strip().lower()
            if cmd == 'q':
                break
    except KeyboardInterrupt:
        pass

    # 清理
    print("\n正在退出...")
    ptt.stop()
    music.stop()
    intent_router.unload()

    # 输出统计
    print("\n" + "=" * 60)
    print("测试统计")
    print("=" * 60)
    print(f"总次数: {stats['total']}")
    print(f"Intent 成功: {stats['intent_ok']}")
    if stats["latencies"]:
        lats = sorted(stats["latencies"])
        print(f"E2E P50: {lats[len(lats)//2]:.0f}ms")
        print(f"E2E P95: {lats[int(len(lats)*0.95)]:.0f}ms")
        print(f"E2E 平均: {sum(lats)/len(lats):.0f}ms")


if __name__ == "__main__":
    main()
