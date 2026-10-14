"""
INTENT-NEGATION-01: "停止播放"误命中搜歌修复测试（修3）

场景：
① 停止播放 → system.unknown（不再搜"停止"）
② 播放停止 → system.unknown
③ 播放停止吧 → 句尾语气词清理后 → system.unknown
④ 不要播放 → system.unknown（现状回归，不破坏）
⑤ 播放不要说话 → MUSIC_SEARCH_PLAY query="不要说话"（歌名不误伤）
⑥ 播放别问我是谁 → MUSIC_SEARCH_PLAY query="别问我是谁"（歌名不误伤）
⑦ 取消所有计时 → cooldown.cancel_all（不被全局否定词误伤）
⑧ 关掉高潮模式 → MUSIC_HIGHLIGHT_MODE enabled=False（不被全局否定词误伤）
⑨ 播放 → MUSIC_RESUME（锚定整串，不进 search）
⑩ 播放下一首 → MUSIC_NEXT（MUSIC_RULES 规则表先于 SEARCH 命中，正确行为）
"""
from app.core.command_bus import CommandType
from app.voice.intent import IntentRouter

router = IntentRouter({})


def _parse(text):
    return router.parse(text)


def test_stop_play_unknown():
    assert _parse("停止播放").intent_type == "system.unknown"


def test_play_stop_unknown():
    assert _parse("播放停止").intent_type == "system.unknown"


def test_play_stop_ba_unknown():
    assert _parse("播放停止吧").intent_type == "system.unknown"


def test_dont_play_unknown():
    assert _parse("不要播放").intent_type == "system.unknown"


def test_play_dont_speak_searches():
    intent = _parse("播放不要说话")
    assert intent.intent_type == CommandType.MUSIC_SEARCH_PLAY
    assert intent.params["query"] == "不要说话"


def test_play_dont_ask_who_i_am_searches():
    intent = _parse("播放别问我是谁")
    assert intent.intent_type == CommandType.MUSIC_SEARCH_PLAY
    assert intent.params["query"] == "别问我是谁"


def test_cancel_all_timers_unchanged():
    assert _parse("取消所有计时").intent_type == "cooldown.cancel_all"


def test_highlight_mode_off_unchanged():
    intent = _parse("关掉高潮模式")
    assert intent.intent_type == CommandType.MUSIC_HIGHLIGHT_MODE
    assert intent.params.get("enabled") is False


def test_play_resume_unchanged():
    assert _parse("播放").intent_type == CommandType.MUSIC_RESUME


def test_play_next_is_music_next():
    # "播放下一首"在 MUSIC_NEXT 规则表内（intent.py MUSIC_RULES 含"播放下一首"），
    # 正确行为是 music.next，不是 unknown
    assert _parse("播放下一首").intent_type == CommandType.MUSIC_NEXT
