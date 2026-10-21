"""语音"清空队列"意图测试：MUSIC_CLEAR_QUEUE 规则 + 不吞 COOLDOWN/alarm 规则。

确认矩阵：
- "清空队列/清空播放列表/清除队列/清空列表/清空歌单" → MUSIC_CLEAR_QUEUE
- "取消所有计时" → cooldown.cancel_all（现有 COOLDOWN，不被新规则吞）
- "清空计时" → system.unknown（COOLDOWN 只认"取消"前缀；新词表不含"计时"）
- "停止播放" → system.unknown（否定词短路不受影响）
"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.command_bus import CommandType  # noqa: E402
from app.voice.intent import IntentRouter  # noqa: E402

router = IntentRouter({})


def _parse(text):
    return router.parse(text)


def test_clear_queue():
    assert _parse("清空队列").intent_type == CommandType.MUSIC_CLEAR_QUEUE


def test_clear_playlist():
    assert _parse("清空播放列表").intent_type == CommandType.MUSIC_CLEAR_QUEUE


def test_clear_list():
    assert _parse("清空列表").intent_type == CommandType.MUSIC_CLEAR_QUEUE


def test_cancel_all_timers_untouched():
    assert _parse("取消所有计时").intent_type == "cooldown.cancel_all"


def test_clear_timer_not_swallowed():
    # "清空计时"不匹配清空队列词表（含"计时"）→ 保持现状 system.unknown
    assert _parse("清空计时").intent_type == "system.unknown"


def test_stop_play_still_unknown():
    assert _parse("停止播放").intent_type == "system.unknown"
