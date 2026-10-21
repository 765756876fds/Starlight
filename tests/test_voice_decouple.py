"""
VOICE-DECOUPLE-01: 采集/识别解耦（方案 A）单测

覆盖：
- start 清空残留队列（stop 后残留段不被处理 → 不触发旧命令）
- stop 后立即 start（代际标记：旧识别线程退出、新线程正常、无重复处理）
- 队列满丢最旧（用户最新指令优先）
- 识别线程异常兜底（线程不退出、后续段仍被处理 → 故障隔离）
- stop 干净退出（识别线程结束、队列清空）
- 长任务白名单 execute_async（MUSIC_SEARCH_PLAY / MUSIC_RESCAN / PLAYER_QUERY）
- 短命令保持 execute（保序、兼容 test_voice_dispatch）
"""
import pytest

pytestmark = pytest.mark.unit

import queue
import time
from unittest.mock import MagicMock

from app.voice.runtime import VoiceRuntime
from app.voice.intent import Intent
from app.core.command_bus import CommandType


def _make_runtime():
    """构造隔离的 VoiceRuntime：替换全局单例依赖为 mock，避免污染其他测试"""
    rt = VoiceRuntime({"asr": {}, "intent": {}}, tts_provider=None)
    rt.asr = MagicMock()
    rt.asr.transcribe.return_value = ""
    rt.event_bus = MagicMock()
    rt.command_bus = MagicMock()
    return rt


def test_start_clears_stale_queue():
    """问题1：start 清空残留队列 —— stop 后残留段（sentinel 之后的竞态段）不被处理"""
    rt = _make_runtime()
    # 模拟 stop 后残留：队列里已有未处理段（stop 排空时采集线程竞态 put 的场景）
    rt._speech_queue.put_nowait((b"stale1", 16000))
    rt._speech_queue.put_nowait((b"stale2", 16000))
    rt.start()
    # start 已清空队列
    assert rt._speech_queue.empty()
    # 给识别线程一个取段窗口：残留段不得被处理（无事件发布、无命令执行）
    time.sleep(0.6)
    rt.event_bus.publish.assert_not_called()
    rt.command_bus.execute.assert_not_called()
    rt.stop()


def test_restart_after_stop():
    """问题1+代际：stop 后立即 start —— 旧段处理一次、新段正常处理、无残留重复触发"""
    rt = _make_runtime()
    rt.asr.transcribe.return_value = "播放下一首"
    rt.intent_router = MagicMock()
    rt.intent_router.parse.return_value = Intent(
        CommandType.MUSIC_NEXT, confidence=0.99, params={}
    )

    rt.start()
    rt._enqueue_speech(b"seg1", 16000)
    time.sleep(0.5)          # seg1 被识别线程处理 → execute 1 次
    rt.stop()                # 排空 + sentinel → 识别线程退出
    rt.start()               # 立即重启：join 旧线程 + 清空 + 新识别线程
    rt._enqueue_speech(b"seg2", 16000)
    time.sleep(0.6)          # seg2 被新识别线程处理 → execute 2 次

    # 恰好 2 次：seg1 + seg2；无残留段被重复处理（若有，会 >2）
    assert rt.command_bus.execute.call_count == 2
    assert rt.event_bus.publish.call_count >= 1
    rt.stop()


def test_queue_full_drops_oldest():
    """队列满丢最旧：最新指令优先"""
    rt = _make_runtime()
    rt._speech_queue = queue.Queue(maxsize=2)
    rt._enqueue_speech(b"first", 16000)
    rt._enqueue_speech(b"second", 16000)
    rt._enqueue_speech(b"third", 16000)
    assert rt._speech_queue.qsize() == 2
    seg1 = rt._speech_queue.get_nowait()
    seg2 = rt._speech_queue.get_nowait()
    assert seg1[0] == b"second"   # 最旧 b"first" 被丢
    assert seg2[0] == b"third"


def test_asr_crash_collect_continues():
    """识别线程异常兜底：打印后继续，线程不退出（故障隔离，采集不受影响）"""
    rt = _make_runtime()
    rt.asr.transcribe.side_effect = RuntimeError("asr boom")
    rt.start()
    rt._enqueue_speech(b"seg1", 16000)
    rt._enqueue_speech(b"seg2", 16000)
    time.sleep(0.8)
    assert rt._asr_thread.is_alive()              # 异常后线程仍存活
    assert rt.asr.transcribe.call_count >= 2      # 两条段都被尝试处理（第2条未被异常打断）
    rt.stop()


def test_stop_threads_clean():
    """stop 干净退出：识别线程结束、队列清空（sentinel 被消费）"""
    rt = _make_runtime()
    rt.start()
    rt._enqueue_speech(b"seg1", 16000)
    time.sleep(0.3)
    rt.stop()
    rt._asr_thread.join(timeout=2)
    assert not rt._asr_thread.is_alive()
    assert rt._speech_queue.empty()


def _make_exec_runtime():
    """轻量构造（同 test_voice_dispatch 风格）：只测 _execute_intent 分发"""
    rt = VoiceRuntime.__new__(VoiceRuntime)
    rt.command_bus = MagicMock()
    rt.event_bus = MagicMock()
    return rt


def test_search_play_async():
    """长任务白名单：MUSIC_SEARCH_PLAY → execute_async（不阻塞识别线程）"""
    rt = _make_exec_runtime()
    intent = Intent(CommandType.MUSIC_SEARCH_PLAY, confidence=0.99,
                    params={"query": "周杰伦"})
    rt._execute_intent(intent, "播放周杰伦")
    rt.command_bus.execute_async.assert_called_once()
    rt.command_bus.execute.assert_not_called()


def test_rescan_async():
    """长任务白名单：MUSIC_RESCAN → execute_async（大库扫描数分钟不卡识别线程）"""
    rt = _make_exec_runtime()
    intent = Intent(CommandType.MUSIC_RESCAN, confidence=0.99, params={})
    rt._execute_intent(intent, "重新扫描音乐库")
    rt.command_bus.execute_async.assert_called_once()
    rt.command_bus.execute.assert_not_called()


def test_player_query_async():
    """长任务白名单：PLAYER_QUERY → execute_async"""
    rt = _make_exec_runtime()
    intent = Intent(CommandType.PLAYER_QUERY, confidence=0.99, params={})
    rt._execute_intent(intent, "查询玩家信息")
    rt.command_bus.execute_async.assert_called_once()
    rt.command_bus.execute.assert_not_called()


def test_short_command_still_sync():
    """短命令保持 execute：保序、兼容现有 test_voice_dispatch"""
    rt = _make_exec_runtime()
    intent = Intent(CommandType.MUSIC_NEXT, confidence=0.99, params={})
    rt._execute_intent(intent, "下一首")
    rt.command_bus.execute.assert_called_once()
    rt.command_bus.execute_async.assert_not_called()
