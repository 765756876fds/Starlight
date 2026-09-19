"""A5 EconomyAdvisor - 把确定性经济状态边沿事件送入现有 SpeechQueue。

不新建 polling/scheduler/TTS。复用 game.time_update 驱动。
仅当状态真正跨边界时 enqueue 一句短文本；priority=B（不抢占 R/Objective 高优提醒）。
"""
import time

from ..speech.speech_queue import SpeechItem, PRIORITY_B
from .current_game_analysis import economy
from .economy_state_detector import EconomyStateDetector


def make_economy_advisor(snapshot_provider, speech_queue, enabled: bool = True):
    detector = EconomyStateDetector()

    def on_time_update(_evt=None):
        if not enabled:
            return
        try:
            snap = snapshot_provider() if snapshot_provider else None
            if snap is None:
                return
            e = economy(snap)
            if e["our_n"] == 0:
                return
            # 数据边界收口：2999 playerlist 不提供双方当前金币（gold 为空）。
            # 双方金币都为 0 = 数据不可得，此时绝不把缺失当 0 去判断经济状态。
            # 安全静默：不更新 EconomyState、不产生 proactive 播报。
            if e["our_total"] == 0 and e["enemy_total"] == 0:
                detector.reset()
                return
            text = detector.update(int(e["diff"]))
            if text and speech_queue is not None:
                speech_queue.enqueue(SpeechItem(
                    text=text,
                    priority=PRIORITY_B,
                    expire_at=time.time() + 20.0,
                    category="economy_state",
                    interruptible=True,
                ))
        except Exception:
            pass

    def on_game_reset(_evt=None):
        detector.reset()

    return {
        "on_time_update": on_time_update,
        "on_game_reset": on_game_reset,
        "detector": detector,
    }
