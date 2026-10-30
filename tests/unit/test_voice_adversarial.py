"""Positive/negative semantic pairs and deterministic delivery races."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path
import re

import pytest

from app.core.command_bus import Command, CommandBus, CommandType as C
from app.voice.intent import Intent, IntentRouter
from app.voice.runtime import VoiceRuntime


def runtime():
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.asr = MagicMock()
    rt.event_bus = MagicMock()
    rt.command_bus = CommandBus()
    rt._enqueue_speak = MagicMock()
    return rt


@pytest.mark.parametrize("text", [
    "你知道怎么取消所有技能计时吗", "如何设置十秒提醒我", "能否帮我记一下石头人闪现",
    "石头人闪现用了是什么意思", "对面石头人R用了是什么意思",
    "我不想取消所有技能计时", "有人问十秒后提醒我怎么用",
    "-10秒提醒我喝水", "十十秒提醒我喝水", "零秒提醒我",
])
def test_question_discussion_and_invalid_duration_never_write(text):
    rt = runtime()
    intent = rt.intent_router.parse(text)
    assert intent.intent_type in ("system.unknown", "system.clarify")
    rt._execute_intent(intent, text)
    assert rt.command_bus.get_history() == []
    assert not any(call.args[0].event_type.startswith("alarm.")
                   for call in rt.event_bus.publish.call_args_list)


@pytest.mark.parametrize("text,command", [("取消所有技能计时", C.COOLDOWN_CANCEL_ALL),
    ("十秒提醒我喝水", "alarm.custom"), ("帮我记一下石头人闪现", "alarm.flash"),
    ("对面石头人R用了", C.COOLDOWN_START)])
def test_positive_counterparts_remain_commands(text, command):
    assert IntentRouter({}).parse(text).intent_type == command


@pytest.mark.parametrize("number", ["三四", "十十", "一百二", "一点", "一二点五"])
def test_malformed_numeric_reply_does_not_become_a_different_value(number):
    router = IntentRouter({})
    router.parse("电影音量")
    assert router.parse(number).intent_type == "system.unknown"


@pytest.mark.parametrize("reply", ["取消", "算了", "不用了", "取消设置"])
def test_cancel_pending_parameter_does_not_delete_unrelated_reminders(reply):
    rt = runtime()
    rt.intent_router.parse("电影音量")
    result = rt.intent_router.parse(reply)
    rt._execute_intent(result, reply)
    assert rt.command_bus.get_history() == []
    assert not any(call.args[0].event_type.startswith("alarm.")
                   for call in rt.event_bus.publish.call_args_list)
    assert rt.intent_router.parse("五十").intent_type == "system.unknown"


def test_parameter_deadline_is_exclusive(monkeypatch):
    monkeypatch.setattr("app.voice.intent.time.monotonic", lambda: 100)
    router = IntentRouter({})
    router.parse("电影音量")
    monkeypatch.setattr("app.voice.intent.time.monotonic", lambda: 112)
    assert router.parse("五十").intent_type == "system.unknown"


@pytest.mark.parametrize("text", ["播放晴天并打开自动排队", "播放晴天和打开自动排队"])
def test_mixed_commands_without_punctuation_are_not_searched_as_song_title(text):
    assert IntentRouter({}).parse(text).intent_type == "system.clarify"


def test_search_completion_is_consumed_once_even_if_delivered_twice():
    rt = runtime()
    callback = rt._async_feedback(C.MUSIC_SEARCH_PLAY, 0)
    callback({"title": "Song"}, None)
    callback(None, RuntimeError("late duplicate"))
    rt._enqueue_speak.assert_called_once()


def test_command_guard_skips_stale_job_before_handler():
    bus = CommandBus()
    handler = MagicMock()
    bus.register(C.MUSIC_SEARCH_PLAY, handler)
    bus.execute(Command(C.MUSIC_SEARCH_PLAY, is_current=lambda: False))
    handler.assert_not_called()


def test_music_volume_handler_passes_requested_step():
    from app.music.controller import MusicController
    bus = CommandBus()
    ctrl = SimpleNamespace(command_bus=bus)
    for name in ("play", "pause", "resume", "toggle", "next", "prev", "volume_up", "volume_down",
                 "jump_to_highlight", "rescan", "cleanup_missing", "clear_queue", "stop_current",
                 "set_speed", "set_sleep_timer", "cancel_sleep_timer", "_cmd_volume_set", "_cmd_seek",
                 "_cmd_search_play", "_cmd_highlight_mode", "_cmd_set_play_mode", "_cmd_play_playlist",
                 "_cmd_create_playlist", "_cmd_add_to_playlist", "_cmd_add_next", "_cmd_play_rank_index"):
        setattr(ctrl, name, MagicMock())
    MusicController._register_commands(ctrl)
    bus.execute(Command(C.MUSIC_VOLUME_UP, params={"step": 20}))
    ctrl.volume_up.assert_called_once_with(20)


@pytest.mark.parametrize("value", [None, {}, {"ok": True, "enabled": False}])
def test_unconfirmed_automation_does_not_claim_enabled(value):
    rt = runtime()
    rt.command_bus.register(C.AUTO_QUEUE_SET, lambda command: value)
    rt._execute_intent(Intent(C.AUTO_QUEUE_SET, params={"enabled": True}), "打开自动排队")
    assert "已开启" not in rt._enqueue_speak.call_args.args[0]


def test_same_audio_is_allowed_again_after_dedup_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.voice.runtime.time.monotonic", lambda: clock[0])
    rt = runtime()
    handler = MagicMock()
    rt.command_bus.register(C.MUSIC_NEXT, handler)
    rt.asr.transcribe.return_value = "下一首"
    rt._process_speech(b"same", 16000)
    clock[0] = 102.0
    rt._process_speech(b"same", 16000)
    assert handler.call_count == 2


@pytest.mark.parametrize("text", ['"取消所有技能计时"', '“打开自动排队”', "打开自动排队？", "关闭自动连招?"])
def test_quoted_or_questioned_game_action_needs_clarification(text):
    assert IntentRouter({}).parse(text).intent_type == "system.clarify"


@pytest.mark.parametrize("text", ["十秒提醒我是什么意思", "十秒提醒我怎么用", "—10秒提醒我", "－10秒提醒我", "−10秒提醒我", "—十秒提醒我", "— 10秒提醒我"])
def test_explanations_and_negative_sign_variants_never_create_timer(text):
    assert IntentRouter({}).parse(text).intent_type == "system.unknown"


@pytest.mark.parametrize("number,expected", [("零", 0), ("五十", 50), ("九十九", 99), ("一百", 100), ("一百零一", 101)])
def test_strict_chinese_numbers_keep_unambiguous_values(number, expected):
    from app.voice.numbers import parse_integer
    assert parse_integer(number) == expected


@pytest.mark.parametrize("text", ["音量5。音量50。", "暂停音乐。不要暂停音乐。", "播放电影X。播放X。"])
def test_folding_never_erases_changed_parameter_negation_or_domain(text):
    from app.voice.runtime import fold_duplicated_text
    rt = runtime()
    rt.asr.transcribe.return_value = text
    rt._process_speech(text.encode(), 16000)
    assert rt.command_bus.get_history() == []
    assert len(fold_duplicated_text(text).split("。")) == len(text.split("。"))


def test_equivalent_pause_wording_still_folds_once():
    from app.voice.runtime import fold_duplicated_text
    assert fold_duplicated_text("暂停播放。暂停。暂停。") == "暂停播放。"


def test_pending_tts_feedback_is_skipped_after_new_request_without_clearing_other_speech():
    from app.speech.speech_queue import SpeechItem, SpeechQueue, PRIORITY_B
    rt = runtime()
    tts = MagicMock()
    queue = SpeechQueue(tts)
    queue._wait_for_playback_with_interrupt_check = MagicMock()
    rt.speech_queue_provider = lambda: queue
    VoiceRuntime._enqueue_speak(rt, "old result", "voice_search_result")
    old = queue._pending[0][2]
    rt._voice_request_generation += 1
    queue.enqueue(SpeechItem("game reminder", PRIORITY_B, time.time() + 30))
    queue._running = True
    try:
        queue._worker_iteration()
        assert old.status == "cancelled"
        tts.speak.assert_not_called()
        queue._worker_iteration()
        tts.speak.assert_called_once_with("game reminder")
    finally:
        queue._running = False


def test_speech_freshness_guard_does_not_run_under_queue_lock():
    from app.speech.speech_queue import SpeechItem, SpeechQueue, PRIORITY_B
    queue = SpeechQueue(MagicMock())
    queue._wait_for_playback_with_interrupt_check = MagicMock()
    queue.enqueue(SpeechItem("test", PRIORITY_B, time.time() + 30,
        is_current=lambda: queue.size() == 0))
    queue._running = True
    worker = threading.Thread(target=queue._worker_iteration, daemon=True)
    try:
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
        queue.tts.speak.assert_called_once_with("test")
    finally:
        queue._running = False


def test_guard_failure_skips_job_without_invoking_handler():
    bus = CommandBus()
    def guard():
        raise RuntimeError("guard unavailable")
    handler = MagicMock()
    bus.register(C.MUSIC_SEARCH_PLAY, handler)
    with pytest.raises(RuntimeError):
        bus.execute(Command(C.MUSIC_SEARCH_PLAY, is_current=guard))
    handler.assert_not_called()


def test_guard_rechecked_after_middleware_invalidates_request():
    bus = CommandBus()
    current = [True]
    bus.add_middleware(lambda c: current.__setitem__(0, False))
    handler = MagicMock()
    bus.register(C.MUSIC_SEARCH_PLAY, handler)
    bus.execute(Command(C.MUSIC_SEARCH_PLAY, is_current=lambda: current[0]))
    handler.assert_not_called()


@pytest.mark.parametrize("value,word", [(False, "未成功"), (None, "无法确认")])
def test_speed_execution_failure_is_not_reported_as_unsupported(value, word):
    rt = runtime()
    rt.command_bus.register(C.MUSIC_SET_SPEED, lambda c: value)
    rt._execute_intent(Intent(C.MUSIC_SET_SPEED, params={"speed": 1.5}), "倍速一点五倍")
    assert word in rt._enqueue_speak.call_args.args[0]
    assert "不支持" not in rt._enqueue_speak.call_args.args[0]


def test_unsupported_speed_does_not_reach_player():
    rt = runtime()
    rt._execute_intent(Intent(C.MUSIC_SET_SPEED, params={"speed": 0.8}), "倍速零点八倍")
    assert rt.command_bus.get_history() == []
    assert "不支持" in rt._enqueue_speak.call_args.args[0]


def test_missing_hook_status_is_not_announced_as_ready():
    rt = runtime()
    rt.command_bus.register(C.AUTO_STATUS_QUERY, lambda c: {"available": True, "enabled": True})
    rt._execute_intent(Intent(C.AUTO_STATUS_QUERY, params={"feature": "combo"}), "自动连招开了吗")
    assert "监听器状态无法确认" in rt._enqueue_speak.call_args.args[0]


def test_online_add_next_consumes_source_mapping_not_list_index():
    from app.music.controller import MusicController
    track = SimpleNamespace(title="Song", artist="Artist", source_name="test", track_id="1", extra={})
    ctrl = SimpleNamespace(search=SimpleNamespace(search=lambda *a, **k: []),
        online_manager=SimpleNamespace(search_all=lambda q: {"test": [track]}),
        add_next=MagicMock(return_value=True))
    assert MusicController._cmd_add_next(ctrl, Command(C.MUSIC_ADD_NEXT, params={"query": "Song"})) is True
    assert ctrl.add_next.call_args.args[0]["track_id"] == "1"


@pytest.mark.parametrize("existing,expected", [([], False), ([{"source_type": "local", "music_id": 7}], True)])
def test_failed_playlist_write_is_only_idempotent_if_record_exists(tmp_path, existing, expected):
    from app.music.controller import MusicController
    song_file = tmp_path / "test.wav"
    song_file.write_bytes(b"test")
    ctrl = SimpleNamespace(get_playlists=lambda: [{"id": 1, "name": "Test"}],
        search=SimpleNamespace(search=lambda *a, **k: [{"id": 7, "path": str(song_file)}]),
        add_to_playlist=MagicMock(return_value=False), get_playlist_songs=lambda pid: existing)
    result = MusicController._cmd_add_to_playlist(ctrl, Command(C.MUSIC_ADD_TO_PLAYLIST,
        params={"name": "Test", "song_query": "Song"}))
    assert result is expected


def test_unavailable_music_volume_is_not_silent():
    from app.music.controller import MusicController
    ctrl = SimpleNamespace(_mpv_available=False)
    rt = runtime()
    rt.command_bus.register(C.MUSIC_VOLUME_SET, lambda c: MusicController.set_volume(ctrl, c.params["volume"]))
    rt._execute_intent(Intent(C.MUSIC_VOLUME_SET, params={"volume": 50}), "音量五十")
    assert "未成功" in rt._enqueue_speak.call_args.args[0]


def test_concurrent_completion_callbacks_only_speak_once():
    rt = runtime()
    callback = rt._async_feedback(C.MUSIC_SEARCH_PLAY, 0)
    start = threading.Barrier(3)
    def run():
        start.wait(timeout=3)
        callback({"title": "Song"}, None)
    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait(timeout=3)
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    rt._enqueue_speak.assert_called_once()


@pytest.mark.parametrize("value", [None, "false", 0, {}])
def test_automation_requires_boolean_parameter_before_execution(value):
    rt = runtime()
    rt._execute_intent(Intent(C.AUTO_QUEUE_SET, params={"enabled": value}), "自动排队")
    assert rt.command_bus.get_history() == []
    assert "打开或关闭" in rt._enqueue_speak.call_args.args[0]


@pytest.mark.parametrize("value", [-10, 0, 101, True, 1.5])
def test_invalid_volume_step_cannot_reverse_or_overflow_adjustment(value):
    rt = runtime()
    rt._execute_intent(Intent(C.MUSIC_VOLUME_UP, params={"step": value}), "音量调整")
    assert rt.command_bus.get_history() == []


def test_bare_song_object_is_still_explicit_music_domain():
    assert IntentRouter({}).parse("暂停歌").scope == "music"


def test_timer_without_confirmed_result_does_not_claim_success():
    rt = runtime()
    rt.command_bus.register(C.MUSIC_SLEEP_TIMER_SET, lambda c: None)
    rt._execute_intent(Intent(C.MUSIC_SLEEP_TIMER_SET, params={"seconds": 1800}), "睡眠定时三十分钟")
    assert "无法确认" in rt._enqueue_speak.call_args.args[0]


@pytest.mark.parametrize("text,expected", [("播放石头人闪现了", C.MUSIC_SEARCH_PLAY),
    ("听石头人闪现了", C.MUSIC_SEARCH_PLAY), ("播放电影石头人R用了", C.MOVIE_SEARCH_PLAY),
    ("播放歌曲闪现冷却", C.MUSIC_SEARCH_PLAY)])
def test_media_title_with_game_keywords_never_creates_game_timer(text, expected):
    assert IntentRouter({}).parse(text).intent_type == expected


@pytest.mark.parametrize("text", ["1" * 1000, "十" * 1000])
def test_excessively_long_numeric_reply_does_not_overflow_or_execute(text):
    router = IntentRouter({})
    router.parse("电影音量")
    assert router.parse(text).intent_type == "system.unknown"


def test_movie_search_feedback_keeps_guard_until_speech_dequeue():
    from app.movie.movie_controller import MovieController
    from app.speech.speech_queue import SpeechQueue
    current = [True]
    queue = SpeechQueue(MagicMock())
    ctrl = MovieController.__new__(MovieController)
    ctrl._speech_queue = queue
    ctrl._voice_feedback = MagicMock()
    ctrl.search = MagicMock(return_value=[{"vod_name": "test"}])
    ctrl.play = MagicMock(return_value=False)
    command = Command(C.MOVIE_SEARCH_PLAY, params={"query": "test"},
                      is_current=lambda: current[0])
    assert ctrl._cmd_search_play(command) is False
    ctrl.play.assert_called_once_with({"vod_name": "test"})
    messages = [item for _, _, item in queue._pending]
    assert [item.text for item in messages] == ["正在加载：test", "电影加载未成功，请尝试其他片源"]
    current[0] = False
    queue._running = True
    try:
        queue._worker_iteration()
        queue._worker_iteration()
        queue.tts.speak.assert_not_called()
        assert all(item.status == "cancelled" for item in messages)
    finally:
        queue._running = False


def test_deferred_music_search_guard_prevents_start_after_new_command():
    rt = runtime()
    search = MagicMock()
    next_song = MagicMock()
    rt.command_bus.register(C.MUSIC_SEARCH_PLAY, search)
    rt.command_bus.register(C.MUSIC_NEXT, next_song)
    rt.command_bus.execute_async = MagicMock()
    rt._execute_intent(Intent(C.MUSIC_SEARCH_PLAY, params={"query": "test"}), "播放test")
    old = rt.command_bus.execute_async.call_args.args[0]
    assert old.is_current() is True
    rt._execute_intent(Intent(C.MUSIC_NEXT), "下一首")
    rt.command_bus.execute(old)
    next_song.assert_called_once()
    search.assert_not_called()


@pytest.mark.parametrize("value", [0, 1, "false", {}])
def test_malformed_pause_property_never_becomes_confirmed_boolean(value):
    player = MagicMock()
    player.get_property.return_value = value
    assert runtime()._real_paused(lambda: player) is None


def test_speed_timeout_reports_unknown_completion_without_retry():
    rt = runtime()
    handler = MagicMock(side_effect=TimeoutError())
    rt.command_bus.register(C.MUSIC_SET_SPEED, handler)
    rt._execute_intent(Intent(C.MUSIC_SET_SPEED, params={"speed": 1.5}), "倍速一点五倍")
    handler.assert_called_once()
    rt._enqueue_speak.assert_called_once_with("倍速设置超时，暂时无法确认结果", "voice")


@pytest.mark.parametrize("method", ["_global_pause", "_global_resume"])
def test_global_media_feedback_also_expires_while_waiting_for_tts(method):
    from app.speech.speech_queue import SpeechQueue
    rt = runtime()
    queue = SpeechQueue(MagicMock())
    queue._wait_for_playback_with_interrupt_check = MagicMock()
    rt.speech_queue_provider = lambda: queue
    rt._enqueue_speak = lambda text, category: VoiceRuntime._enqueue_speak(rt, text, category)
    getattr(rt, method)()
    assert queue.size() == 1
    rt._voice_request_generation += 1
    queue._running = True
    try:
        queue._worker_iteration()
        queue.tts.speak.assert_not_called()
    finally:
        queue._running = False


def capability_cases():
    document = Path(__file__).resolve().parents[2] / "docs" / "语音能力矩阵.md"
    rows = []
    for line in document.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            columns = [cell.strip() for cell in line.strip("|").split("|")]
            if len(columns) >= 3 and re.fullmatch(r"(?:music|movie|auto|cooldown|alarm|player|system)\.[a-z_]+", columns[2]):
                rows.append((columns[1], columns[2]))
    return rows


@pytest.mark.parametrize("text,command", capability_cases())
def test_capability_matrix_examples_match_real_parser(text, command):
    assert IntentRouter({}).parse(text).intent_type == command


def test_capability_matrix_does_not_accidentally_lose_examples():
    assert len(capability_cases()) == 65
