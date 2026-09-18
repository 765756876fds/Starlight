"""
语音 → 音乐控制 最终 E2E 验收测试
6 个核心命令 × 5 次 = 30 次

链路：
    PTT 麦克风 → Qwen3-ASR → Intent Router → CommandBus → MusicController → mpv

用法：
    python tools/test_e2e_voice_music.py

操作：
    按住空格键说话，松开自动识别并执行
    按提示依次测试每个命令
    输入 q 退出
"""
import os
import sys
import time
import json

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from app.voice.asr import Qwen3ServerASR
from app.voice.intent import IntentRouter
from app.voice.ptt import PTTMicrophone
from app.music.controller import MusicController
from app.core.command_bus import get_command_bus, Command

# 测试计划：6 个命令 × 5 次
TEST_PLAN = [
    ("下一首", "music.next"),
    ("上一首", "music.previous"),
    ("暂停音乐", "music.pause"),
    ("继续播放", "music.resume"),
    ("声音大一点", "music.volume_up"),
    ("声音小一点", "music.volume_down"),
]


def main():
    print("=" * 60)
    print("语音 → 音乐控制 最终 E2E 验收测试")
    print("=" * 60)
    print()

    # ===== 1. ASR =====
    print("[1/4] 初始化 ASR...")
    asr = Qwen3ServerASR(base_url="http://127.0.0.1:8080/v1")
    asr.load()
    if not asr.is_available():
        print("❌ ASR 不可用")
        return
    print()

    # ===== 2. Intent =====
    print("[2/4] 初始化 Intent Router...")
    intent_config = {
        "rule_based_first": True,
        "openai_api": {
            "enabled": False,  # E2E 测试只用 Rule，不需要 LLM
        },
    }
    intent_router = IntentRouter(intent_config)
    intent_router.initialize()
    print()

    # ===== 3. Music =====
    print("[3/4] 初始化 Music Controller...")
    music_config = {
        "database": os.path.join(project_root, "data", "test_music.db"),
        "scan_dirs": [r"D:\落雪\音乐文件"],
        "mpv_path": r"D:\新建文件夹 (2)\mpv.exe",
        "mpv_ipc_pipe": r"\\.\pipe\lol_music_e2e_test",
        "default_volume": 60,
    }
    music = MusicController(music_config)
    music.start()
    print()

    # ===== 4. 播放一首测试音乐 =====
    print("[4/4] 加载测试音乐...")
    songs = music.db.get_random_songs(3)
    if songs:
        music.play_song(songs[0])
        time.sleep(1)
        print(f"当前播放: {songs[0].get('title', '?')}")
    print()

    # ===== 测试记录 =====
    results = []
    current_test = {"cmd_idx": 0, "trial": 0, "expected_intent": None}

    def get_next_test():
        cmd_idx = current_test["cmd_idx"]
        trial = current_test["trial"]
        if cmd_idx >= len(TEST_PLAN):
            return None
        cmd_name, expected = TEST_PLAN[cmd_idx]
        return {
            "cmd_name": cmd_name,
            "expected_intent": expected,
            "trial": trial + 1,
            "total": 5,
        }

    def on_recording_start():
        nxt = get_next_test()
        if nxt:
            print(f"\n[PTT] 录音中... 请说: 「{nxt['cmd_name']}」(第{nxt['trial']}/{nxt['total']}次)")
        else:
            print("\n[PTT] 录音中...")

    def on_recording_stop(audio_data: bytes):
        if len(audio_data) < 16000 * 0.3:
            print("[PTT] 录音太短，忽略")
            return

        nxt = get_next_test()
        if not nxt:
            print("[PTT] 所有测试已完成")
            return

        # ASR
        t0 = time.time()
        text = asr.transcribe(audio_data)
        t_asr = time.time() - t0

        if not text:
            print(f"[ASR] 空结果 | {t_asr*1000:.0f}ms")
            results.append({
                "cmd": nxt["cmd_name"],
                "trial": nxt["trial"],
                "asr_text": "",
                "intent": "",
                "result": "FAILED",
                "asr_ms": t_asr * 1000,
                "intent_ms": 0,
                "cmd_ms": 0,
                "e2e_ms": 0,
            })
            advance_test()
            return

        clean_text = text.strip().rstrip("。，！？")
        print(f"[ASR] {text} | {t_asr*1000:.0f}ms")

        # Intent
        t1 = time.time()
        intent = intent_router.parse(clean_text)
        t_intent = time.time() - t1

        print(f"[Intent] {intent.intent_type} (conf={intent.confidence:.2f}) | {t_intent*1000:.0f}ms")

        # CommandBus
        result = "WRONG"
        t_cmd = 0
        if intent.intent_type == nxt["expected_intent"]:
            try:
                t2 = time.time()
                cmd = Command(
                    command_type=intent.intent_type,
                    params=intent.params,
                    source="voice",
                )
                get_command_bus().execute(cmd)
                t_cmd = time.time() - t2
                print(f"[CommandBus] ✅ 执行成功 | {t_cmd*1000:.0f}ms")

                # 判断结果
                if clean_text == nxt["cmd_name"]:
                    result = "EXACT"
                else:
                    result = "INTENT_OK"
            except Exception as e:
                print(f"[CommandBus] ❌ 执行失败: {e}")
                result = "FAILED"
        else:
            print(f"[CommandBus] ⚠️ 期望 {nxt['expected_intent']}, 实际 {intent.intent_type}")

        e2e = time.time() - t0
        results.append({
            "cmd": nxt["cmd_name"],
            "trial": nxt["trial"],
            "asr_text": clean_text,
            "intent": intent.intent_type,
            "expected": nxt["expected_intent"],
            "result": result,
            "asr_ms": t_asr * 1000,
            "intent_ms": t_intent * 1000,
            "cmd_ms": t_cmd * 1000,
            "e2e_ms": e2e * 1000,
        })

        advance_test()

    def advance_test():
        current_test["trial"] += 1
        if current_test["trial"] >= 5:
            current_test["cmd_idx"] += 1
            current_test["trial"] = 0
            if current_test["cmd_idx"] < len(TEST_PLAN):
                print(f"\n--- 下一个命令: 「{TEST_PLAN[current_test['cmd_idx']][0]}」 ---")
            else:
                print("\n🎉 所有 30 次测试完成！")

    # ===== 启动 PTT =====
    print("=" * 60)
    print("操作说明：")
    print("  - 按住【空格键】开始说话")
    print("  - 松开空格键自动识别并执行")
    print("  - 按提示依次测试每个命令（每个说5次）")
    print("  - 按【Ctrl+C】退出")
    print("=" * 60)
    print()
    print(f"第一个命令: 「{TEST_PLAN[0][0]}」")

    ptt = PTTMicrophone(
        on_recording_start=on_recording_start,
        on_recording_stop=on_recording_stop,
    )
    ptt.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n收到退出信号...")

    # 清理
    print("\n正在退出...")
    ptt.stop()
    music.stop()
    intent_router.unload()

    # ===== 输出报告 =====
    print("\n" + "=" * 60)
    print("E2E 验收报告")
    print("=" * 60)

    # 总览
    total = len(results)
    exact = sum(1 for r in results if r["result"] == "EXACT")
    intent_ok = sum(1 for r in results if r["result"] == "INTENT_OK")
    wrong = sum(1 for r in results if r["result"] == "WRONG")
    failed = sum(1 for r in results if r["result"] == "FAILED")

    print(f"\n总次数: {total}")
    print(f"EXACT: {exact}")
    print(f"INTENT_OK: {intent_ok}")
    print(f"WRONG: {wrong}")
    print(f"FAILED: {failed}")
    print(f"Intent-preserving rate: {(exact + intent_ok) / max(total,1) * 100:.1f}%")

    # 延迟
    e2e_lats = [r["e2e_ms"] for r in results if r["result"] != "FAILED"]
    if e2e_lats:
        e2e_lats.sort()
        print(f"\nE2E 延迟:")
        print(f"  P50: {e2e_lats[len(e2e_lats)//2]:.0f}ms")
        print(f"  P95: {e2e_lats[int(len(e2e_lats)*0.95)]:.0f}ms")
        print(f"  平均: {sum(e2e_lats)/len(e2e_lats):.0f}ms")
        print(f"  最大: {e2e_lats[-1]:.0f}ms")

    # 按命令分类
    print(f"\n按命令分类:")
    for cmd_name, expected in TEST_PLAN:
        cmd_results = [r for r in results if r["cmd"] == cmd_name]
        if not cmd_results:
            continue
        ok = sum(1 for r in cmd_results if r["result"] in ("EXACT", "INTENT_OK"))
        print(f"  {cmd_name}: {ok}/{len(cmd_results)}")
        for r in cmd_results:
            mark = {"EXACT": "✅", "INTENT_OK": "🟡", "WRONG": "❌", "FAILED": "🔴"}[r["result"]]
            print(f"    {mark} #{r['trial']}: ASR=\"{r['asr_text']}\" → {r['intent']} | {r['e2e_ms']:.0f}ms")

    # 保存详细结果
    report_path = os.path.join(project_root, "tools", "e2e_test_results.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存: {report_path}")


if __name__ == "__main__":
    main()
