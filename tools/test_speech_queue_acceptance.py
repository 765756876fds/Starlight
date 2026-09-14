"""
SpeechQueue × TTS 真实验收脚本
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.speech.tts import TTSController
from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_S, PRIORITY_A, PRIORITY_B


def main():
    print("=" * 60)
    print("SpeechQueue × TTS 真实验收")
    print("=" * 60)

    # 初始化 TTS
    tts_config = {
        "engine": "edge",
        "edge_voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "volume": "+0%",
        "output_dir": "./data/tts",
        "mpv_path": r"D:\新建文件夹 (2)\mpv.exe",
        "enabled": True,
    }
    tts = TTSController(tts_config)
    tts.start()

    # 初始化 Queue
    queue = SpeechQueue(tts)
    queue.start()

    # ========== Test A: 连续队列 ==========
    print("\n[A] 连续队列测试（三句顺序播放，无重叠）")
    input("按回车开始...")

    now = time.time()
    queue.enqueue(SpeechItem(
        text="第一条测试语音。",
        priority=PRIORITY_B,
        expire_at=now + 10,
        event_id="test_a1",
    ))
    queue.enqueue(SpeechItem(
        text="第二条测试语音。",
        priority=PRIORITY_B,
        expire_at=now + 10,
        event_id="test_a2",
    ))
    queue.enqueue(SpeechItem(
        text="第三条测试语音。",
        priority=PRIORITY_B,
        expire_at=now + 10,
        event_id="test_a3",
    ))

    # 等待全部完成
    time.sleep(5)
    input("  三句依次播放，没有重叠？(回车继续)")

    # ========== Test B: B 被 A 打断 ==========
    print("\n[B] 打断测试（B 播放中，A 打断）")
    input("按回车开始播放 B...")

    now = time.time()
    queue.enqueue(SpeechItem(
        text="这是一条比较长的语音，用来测试打断能力。如果你完整听完了这句话，说明打断没有生效。",
        priority=PRIORITY_B,
        expire_at=now + 10,
        event_id="test_b1",
    ))

    time.sleep(1)  # 等 B 开始播放
    print("  B 正在播放，按回车加入 A...")
    input()

    now = time.time()
    queue.enqueue(SpeechItem(
        text="高优先级打断测试。",
        priority=PRIORITY_A,
        expire_at=now + 10,
        event_id="test_b2",
    ))

    time.sleep(2)
    input("  B 被停止，A 开始播放？(回车继续)")

    # ========== Test C: A 被 S 打断 ==========
    print("\n[C] 二级打断测试（A 播放中，S 打断）")
    input("按回车开始播放 A...")

    now = time.time()
    queue.enqueue(SpeechItem(
        text="这是 A 级语音。如果你完整听完了，说明二级打断没有生效。",
        priority=PRIORITY_A,
        expire_at=now + 10,
        event_id="test_c1",
    ))

    time.sleep(1)
    print("  A 正在播放，按回车加入 S...")
    input()

    now = time.time()
    queue.enqueue(SpeechItem(
        text="最高优先级 S 级打断。",
        priority=PRIORITY_S,
        expire_at=now + 10,
        event_id="test_c2",
    ))

    time.sleep(2)
    input("  A 被停止，S 开始播放？(回车继续)")

    # ========== Test D: clear_all ==========
    print("\n[D] clear_all 测试")
    input("按回车开始播放 B...")

    now = time.time()
    queue.enqueue(SpeechItem(
        text="这是一条用于测试 clear all 的语音。如果你还在听，说明没有被停止。",
        priority=PRIORITY_B,
        expire_at=now + 10,
        event_id="test_d1",
    ))

    time.sleep(1)
    print("  正在播放，按回车 clear_all...")
    input()
    queue.clear_all()

    time.sleep(0.5)
    input("  声音立即停止了？(回车继续)")

    # ========== 检查残留 ==========
    print("\n[E] 检查残留")
    import subprocess
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq mpv.exe"],
        capture_output=True, text=True
    )
    mpv_count = result.stdout.count("mpv.exe")
    print(f"  残留 mpv 进程: {mpv_count} 个")

    # 清理
    queue.shutdown()
    tts.stop()

    print("\n" + "=" * 60)
    print("验收完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
