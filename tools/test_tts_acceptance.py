"""
TTS 真实机器验收脚本

测试：
A. 单句播放
B. 连续三句
C. 中断测试
D. 反复 stop/play 10次
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.speech.tts import TTSController, TTSState


def main():
    config = {
        "engine": "edge",
        "edge_voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "volume": "+0%",
        "output_dir": "./data/tts",
        "mpv_path": r"D:\新建文件夹 (2)\mpv.exe",
        "enabled": True,
    }

    tts = TTSController(config)
    tts.start()

    print("=" * 50)
    print("TTS 真实机器验收")
    print("=" * 50)

    # ========== A. 单句播放 ==========
    print("\n[A] 单句播放测试")
    input("按回车开始播放...")
    tts.speak("测试语音。这是第一句测试。")
    time.sleep(1)
    print(f"  状态: {tts.get_state().value}")
    tts.wait_finished(timeout=15)
    print(f"  完成状态: {tts.get_state().value}")
    input("  听到声音了吗？(回车继续)")

    # ========== B. 连续三句 ==========
    print("\n[B] 连续三句测试")
    input("按回车开始...")
    tts.speak("第一句测试。")
    time.sleep(0.3)
    tts.speak("第二句测试。")
    time.sleep(0.3)
    tts.speak("第三句测试。")
    tts.wait_finished(timeout=20)
    print(f"  完成状态: {tts.get_state().value}")
    input("  三句都听到了吗？(回车继续)")

    # ========== C. 中断测试 ==========
    print("\n[C] 中断测试")
    input("按回车开始播放长句（播放过程中按回车停止）...")
    tts.speak("这是一个用于测试 TTS 中断能力的较长语音。现在应该可以在播放过程中被停止。如果你听到了完整的这句话，说明中断没有生效。这句话足够长，给你足够的时间来按回车停止。")
    time.sleep(0.5)
    print("  正在播放，按回车停止...")
    input()
    tts.stop_playback()
    print(f"  停止后状态: {tts.get_state().value}")
    input("  声音立即停止了吗？(回车继续)")

    # ========== D. 反复 stop/play ==========
    print("\n[D] 反复 stop/play 10次")
    input("按回车开始...")
    for i in range(10):
        tts.speak(f"第{i+1}次测试。")
        time.sleep(0.3)
        tts.stop_playback()
        time.sleep(0.1)
        print(f"  第{i+1}次完成，状态: {tts.get_state().value}")

    print(f"  最终状态: {tts.get_state().value}")
    input("  没有卡死吧？(回车继续)")

    # ========== 检查残留 ==========
    print("\n[E] 检查残留")
    import subprocess
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq mpv.exe"],
        capture_output=True, text=True
    )
    mpv_count = result.stdout.count("mpv.exe")
    print(f"  残留 mpv 进程: {mpv_count} 个")

    # 检查临时文件
    tts_files = [f for f in os.listdir("./data/tts") if f.endswith(".mp3")]
    print(f"  残留临时文件: {len(tts_files)} 个")

    print("\n" + "=" * 50)
    print("验收完成！")
    print("=" * 50)

    tts.stop()


if __name__ == "__main__":
    main()
