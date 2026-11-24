"""Natural meaning -> real bus/controller/SQLite; isolated MPV and source adapters."""

import threading
import time
from unittest.mock import Mock

import pytest

from app.core.command_bus import Command, CommandBus, CommandType as C
from app.core.operation_result import OperationResult, OperationStatus as S
from app.music.preferences import media_key
from app.voice.interaction_memory import InteractionMemory
from app.voice.natural_control import NaturalControl
from app.voice.natural_language import extract
from app.voice.runtime import VoiceRuntime
from tests.unit.test_media_context import context
from tests.unit.test_movie_resume import controller as movie_fixture
from tests.unit.test_music_recommendation import controller as controller  # noqa: F401


def build(controller, tmp_path):
    c = controller
    c.mpv.is_running = lambda: True
    c.mpv.pause = lambda: c.mpv.set_property("pause", True)
    c.mpv.play = lambda: c.mpv.set_property("pause", False)
    c._resolver = object()
    c._running = True
    rt = VoiceRuntime({})
    rt.command_bus = c.command_bus
    rt.music_controller_provider = lambda: c
    rt.music_mpv_provider = lambda: c.mpv
    rt._enqueue_speak = Mock()
    memory = InteractionMemory(tmp_path / "interactions.db")
    control = rt.configure_natural_media(memory)
    return rt, control


def natural(rt, text):
    meaning = extract(text)
    assert meaning is not None, text
    rt._voice_request_generation += 1
    return rt.command_bus.execute(Command(C.NATURAL_MEDIA, meaning.to_dict()))


def ready(controller):
    assert controller.recommend_music().status is S.CONFIRMED
    assert controller.queue.size() == 10
    assert not controller._is_switching_track


def settle(controller):
    deadline = time.monotonic() + 2
    while controller._is_switching_track and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not controller._is_switching_track


@pytest.mark.parametrize("rating,expected", [("不好听", -0.3), ("一般般", -0.1)])
def test_negative_tracks_original_identity_after_real_next(controller, tmp_path, rating, expected):
    ready(controller)
    rt, control = build(controller, tmp_path)
    before = dict(controller.get_current_song())
    catalogs = controller.online_manager.details
    result = natural(rt, "这首" + rating)
    assert result.status is S.ACCEPTED
    settle(controller)
    assert controller.get_current_song() != before
    assert controller.online_manager.details == catalogs
    assert control.memory.flush(timeout=None)
    assert control.memory.weak_profile("music") == {media_key(before): expected}
    assert media_key(controller.get_current_song()) not in control.memory.weak_profile("music")
    assert controller.music_memory.profile()["explicit"] == {}
    assert control.close()


def test_like_and_save_real_playlist_are_distinct_and_persist(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    song = dict(controller.get_current_song())
    assert natural(rt, "这首不错").status is S.CONFIRMED
    assert controller.get_playlists() == []
    assert controller.music_memory.profile()["explicit"][("track", media_key(song))] == 1
    for _ in range(2):
        assert natural(rt, "这首不错，给我留着").status is S.CONFIRMED
    playlists = controller.get_playlists()
    assert len(playlists) == 1 and playlists[0]["name"] == "我喜欢的音乐"
    songs = controller.db.get_playlist_songs(playlists[0]["id"])
    assert len(songs) == 1 and songs[0]["track_id"] == song["track_id"]
    assert controller.queue.size() == 10
    assert control.close()


def test_correction_revokes_only_immediately_attributable_weak_feedback(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    original = media_key(controller.get_current_song())
    controller.music_memory.set_preference("track", original, 1)
    assert natural(rt, "这首不好听").status is S.ACCEPTED
    settle(controller)
    assert control.memory.flush(timeout=None) and original in control.memory.weak_profile("music")
    result = natural(rt, "不是换歌，是换榜单")
    assert result.status is S.CONFIRMED
    assert controller.queue.size() == 10
    assert control.memory.flush(timeout=None) and original not in control.memory.weak_profile("music")
    assert controller.music_memory.profile()["explicit"][("track", original)] == 1
    assert control.close()


def test_manual_change_makes_old_feedback_ineligible_for_undo(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    assert natural(rt, "这首不好听").status is S.ACCEPTED
    settle(controller)
    controller._play_request_id += 1  # independent, newer manual playback generation
    assert control.memory.flush(timeout=None)
    before = control.memory.weak_profile("music")
    natural(rt, "不是换歌，是换榜单")
    assert control.memory.flush(timeout=None) and control.memory.weak_profile("music") == before
    assert control.close()


def test_style_handoff_keeps_new_recommendation_queue_and_original_playlists(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    controller.create_playlist("手动歌单")
    before = controller.recommendations.session["key"]
    result = natural(rt, "换个感觉")
    assert result.status is S.CONFIRMED
    assert controller.recommendations.session["key"] != before
    assert controller.queue.size() == 10
    assert [p["name"] for p in controller.get_playlists()] == ["手动歌单"]
    assert control.close()


def test_missing_handler_unknown_or_stale_never_claims_success(tmp_path):
    context_, players, controllers, calls = context()
    rt = context_.runtime
    rt.media_context = context_
    rt._voice_request_generation = 1
    c = controllers["music"]
    c._lock = threading.RLock()
    c.get_current_song = lambda: {"title": "A", "music_id": 1}
    control = NaturalControl(rt, InteractionMemory(tmp_path / "habits.db"))
    result = control.execute(Command(C.NATURAL_MEDIA, extract("给我留着").to_dict()))
    assert result.status is S.UNAVAILABLE and not calls
    rt.command_bus.register(C.MUSIC_SAVE_CURRENT, lambda c: None)
    assert (
        control.execute(Command(C.NATURAL_MEDIA, extract("给我留着").to_dict())).status is S.UNKNOWN
    )
    assert (
        control.execute(
            Command(C.NATURAL_MEDIA, extract("这首不好听").to_dict(), is_current=lambda: False)
        ).status
        is S.UNKNOWN
    )
    assert not calls
    players["music"].state = "stopped"
    assert (
        control.execute(Command(C.NATURAL_MEDIA, extract("这首不好听").to_dict())).status
        is S.NEEDS_PARAMETER
    )
    assert control.close()


def test_movie_negative_saves_progress_stops_then_recommends(tmp_path):
    movie = movie_fixture(tmp_path)
    movie._playback_gen = 1
    movie._remember_view = Mock()
    movie.current = lambda: movie._current
    movie.voice_browse_kind = "movie"
    ctx, _, controllers, _ = context("stopped", "playing")
    ctx.runtime.movie_controller_provider = lambda: movie
    ctx.runtime.media_context = ctx
    ctx.runtime._voice_request_generation = 1
    bus = ctx.runtime.command_bus
    bus.register(C.MOVIE_STOP, lambda c: movie.stop(_voice_guard=c.is_current))
    recommended = []
    bus.register(
        C.MOVIE_RECOMMEND,
        lambda c: recommended.append(c.params) or OperationResult(S.CONFIRMED, "已有候选"),
    )
    control = NaturalControl(ctx.runtime, InteractionMemory(tmp_path / "habit.db"))
    result = control.execute(Command(C.NATURAL_MEDIA, extract("这个不好看").to_dict()))
    assert result.status is S.CONFIRMED
    assert [c[0] for c in movie.mpv.calls] == ["save", "stop"]
    assert movie.current() is None and recommended == [{"kind": "movie"}]
    movie._remember_view.assert_called_once_with(force=True)
    assert control.memory.flush(timeout=None) and control.memory.weak_profile("movie")
    assert control.close()


def test_failed_movie_stop_never_offers_replacements_or_negative_memory(tmp_path):
    movie = movie_fixture(tmp_path)
    movie._playback_gen = 1
    movie.mpv.stop_result = False
    movie.current = lambda: movie._current
    ctx, _, _, _ = context("stopped", "playing")
    rt = ctx.runtime
    rt.movie_controller_provider = lambda: movie
    rt.media_context = ctx
    rt._voice_request_generation = 1
    rt.command_bus.register(C.MOVIE_STOP, lambda c: movie.stop(_voice_guard=c.is_current))
    recommend = Mock()
    rt.command_bus.register(C.MOVIE_RECOMMEND, recommend)
    control = NaturalControl(rt, InteractionMemory(tmp_path / "habit.db"))
    assert (
        control.execute(Command(C.NATURAL_MEDIA, extract("这个不好看").to_dict())).status
        is S.FAILED
    )
    recommend.assert_not_called()
    assert control.memory.flush(timeout=None) and control.memory.weak_profile("movie") == {}
    assert movie.current() is not None and control.close()


def test_actual_runtime_routes_rating_and_save_once_without_tts(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    workers = []
    execute = rt.command_bus.execute_async

    def tracked(*args, **kwargs):
        worker = execute(*args, **kwargs)
        workers.append(worker)
        return worker

    rt.command_bus.execute_async = tracked
    for text in ("这首不错，给我留着", "这首歌不好听"):
        rt._execute_intent(rt.intent_router.parse(text), text)
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
    settle(controller)
    assert len(controller.db.get_playlist_songs(controller.get_playlists()[0]["id"])) == 1
    assert len(controller.mpv.loaded) == 2
    rt._enqueue_speak.assert_not_called()
    assert control.close()


@pytest.mark.parametrize(
    "music,movie,focus,expected",
    [
        ("playing", "stopped", None, "music"),
        ("stopped", "playing", None, "movie"),
        ("playing", "playing", None, None),
        ("playing", "playing", "movie", "movie"),
        ("stopped", "stopped", None, None),
        ("unknown", "playing", None, None),
    ],
)
def test_new_paraphrase_and_existing_pause_share_live_arbitration(
    tmp_path, music, movie, focus, expected
):
    ctx, players, _, calls = context(music, movie)
    rt = ctx.runtime
    rt.media_context = ctx
    rt._voice_request_generation = 1
    control = NaturalControl(rt, InteractionMemory(tmp_path / "habit.db"))
    if focus:
        ctx.note_focus(focus)
    result = control.execute(Command(C.NATURAL_MEDIA, extract("暂停会儿").to_dict()))
    assert calls == ([(expected, "pause")] if expected else [])
    assert result.status is (S.CONFIRMED if expected else S.NEEDS_PARAMETER)
    if expected:
        assert players[expected].state == "paused"
        assert (
            control.execute(Command(C.NATURAL_MEDIA, extract("恢复播放").to_dict())).status
            is S.CONFIRMED
        )
        assert players[expected].state == "playing"
    assert control.close()


def test_movie_ownership_waits_for_actual_start_and_never_resumes_music(tmp_path):
    from app.movie.movie_controller import MovieController

    music = type("Music", (), {})()
    music._play_request_id = 1
    music.mpv = Mock()
    music.mpv.get_property.return_value = True
    music.pause = Mock(return_value=True)
    music.resume = Mock()
    movie = MovieController.__new__(MovieController)
    movie._music, movie.mpv = music, Mock()
    movie.mpv.get_property.return_value = False
    movie._playback_gen = 1
    movie._last_idle = False
    movie._stopping = False
    movie._current = {"vod_name": "隔离影片"}
    movie._external_url_mode = False
    done = threading.Event()
    music.pause.side_effect = lambda **kw: done.set() or kw["_voice_guard"]()
    movie._pause_music_after_start(True)
    movie._pause_music_after_start(None)
    music.pause.assert_not_called()
    movie._pause_music_after_start(False)
    assert done.wait(1)
    music.pause.assert_called_once()
    music.resume.assert_not_called()


def test_legacy_context_commands_can_confirm_a_reliable_habit(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    workers = []
    execute = rt.command_bus.execute_async

    def tracked(*args, **kwargs):
        worker = execute(*args, **kwargs)
        workers.append(worker)
        return worker

    rt.command_bus.execute_async = tracked
    for text in ("先停一下", "对就这样"):
        rt._execute_intent(rt.intent_router.parse(text), text)
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
    assert controller.mpv.get_property("pause") is True
    assert control.memory.flush(timeout=None)
    assert len([r for r in control.memory._cache if r[6] == "confirmation"]) == 1
    assert control.memory.preferred_domain("pause:current::", ["music", "movie"]) is None
    assert control.close()


@pytest.mark.parametrize(
    "states", [("stopped", "playing"), ("stopped", "paused"), ("playing", "paused")]
)
def test_subtitle_semantics_reuses_actual_media_snapshot_and_tuple_result(tmp_path, states):
    from app.movie.movie_controller import MovieController
    from tests.unit.test_movie_subtitle_controls import SubtitleMpv

    movie = MovieController.__new__(MovieController)
    movie.mpv = SubtitleMpv("isolated-video.mp4")
    movie._playback_gen = 1
    movie._current = {"first_url": "isolated-video.mp4"}
    movie._subtitle_lock = threading.RLock()
    movie.mpv.add_track("isolated-subtitle.srt")
    ctx, _, _, _ = context(*states)
    rt = ctx.runtime
    rt.media_context = ctx
    rt.movie_controller_provider = lambda: movie
    rt._voice_request_generation = 1
    control = NaturalControl(rt, InteractionMemory(tmp_path / "habit.db"))
    result = control.execute(Command(C.NATURAL_MEDIA, extract("隐藏字幕").to_dict()))
    assert result.status is S.CONFIRMED
    assert movie.mpv.props["sub-visibility"] is False
    result = control.execute(Command(C.NATURAL_MEDIA, extract("显示字幕").to_dict()))
    assert result.status is S.CONFIRMED and movie.mpv.props["sub-visibility"] is True
    assert control.close()


def test_busy_music_lock_is_bounded_and_stale_action_does_not_cancel_recommendation(
    controller, tmp_path
):
    ready(controller)
    rt, control = build(controller, tmp_path)
    before = controller.recommendations._generation
    assert controller.pause(_voice_guard=lambda: False) is False
    assert controller.recommendations._generation == before
    entered, release = threading.Event(), threading.Event()

    def hold():
        with controller._lock:
            entered.set()
            assert release.wait(1)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(0.5)
    started = time.monotonic()
    try:
        assert controller.pause(_voice_guard=lambda: True) is False
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive() and control.close()


def test_unqualified_continue_uses_all_reliable_kinds_and_honors_yesterday(tmp_path):
    from app.movie.movie_controller import MovieController
    from tests.unit.test_film_recommendation import service, film

    movie = MovieController.__new__(MovieController)
    movie.recommendations = service(tmp_path)
    movie._recommendation_ready = Mock()
    work = film(5, kind="anime")
    movie.recommendations.memory.observe(work, {"first_url": "https://fixture.invalid/5"}, 40, 100)
    result = movie._cmd_recommend(Command(C.MOVIE_CONTINUE, {"kind": "mixed"}), resume=True)
    assert result.status is S.ACCEPTED
    batch, current, _ = movie._recommendation_ready.call_args.args[0]
    assert len(batch.rows) == 1 and batch.rows[0]["kind"] == "anime"
    assert batch.rows[0]["resume_seconds"] == 40 and current()
    assert batch.action == "resume"
    result = movie._cmd_recommend(
        Command(C.MOVIE_CONTINUE, {"kind": "mixed", "period": "yesterday"}), resume=True
    )
    assert result.status is S.UNAVAILABLE
    batch, _, _ = movie._recommendation_ready.call_args.args[0]
    assert not batch.rows


@pytest.mark.parametrize("text", ["这首不好听但是喜欢", "这首一般般真棒", "这首难听给我留着"])
def test_conflicting_evaluations_and_negative_save_never_produce_action(text):
    assert extract(text) is None


def test_weak_feedback_changes_real_ranking_but_explicit_like_wins(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    original = dict(controller.get_current_song())
    key = media_key(original)
    assert control.memory.record(
        "test-negative",
        feature="evaluate:music:negative:",
        domain="music",
        action="next",
        kind="weak",
        media_key=key,
        rating="negative",
    )
    assert control.memory.flush(timeout=None)
    choices = controller.recommendations.choices(controller.online_manager)
    first = choices[0].tracks[choices[0].index]
    assert first.track_id != original["track_id"]
    assert natural(rt, "这首不错").status is S.CONFIRMED
    choices = controller.recommendations.choices(controller.online_manager)
    assert choices[0].tracks[choices[0].index].track_id == original["track_id"]
    assert control.close()


def test_like_survives_independent_save_failure(controller, tmp_path, monkeypatch):
    ready(controller)
    rt, control = build(controller, tmp_path)
    key = media_key(controller.get_current_song())
    monkeypatch.setattr(controller.db, "save_liked_song", lambda *a: False)
    result = natural(rt, "这首不错给我留着")
    assert result.status is S.FAILED and "已记住喜欢" in result.message
    assert controller.music_memory.profile()["explicit"][("track", key)] == 1
    assert controller.get_playlists() == []
    assert control.memory.flush(timeout=None)
    assert control.close()


def test_lazy_initialization_is_single_and_stop_invalidates_before_flushing(tmp_path):
    rt = VoiceRuntime({})
    rt.command_bus = CommandBus()
    memory = InteractionMemory(tmp_path / "habits.db")
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(rt.configure_natural_media(memory)))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    assert len({id(r) for r in results}) == 1
    control = results[0]
    closing = control.close
    before = rt._voice_request_generation
    seen = []

    def close():
        seen.append(rt._voice_request_generation)
        return closing()

    control.close = close
    rt.asr.unload_all = Mock()  # never touch a real production recognition service
    rt.stop()
    assert seen == [before + 1]
    assert rt._natural_media is None and control._closed


def test_neutral_movie_evaluation_records_light_feedback_without_stopping(tmp_path):
    movie = movie_fixture(tmp_path)
    movie._playback_gen = 1
    ctx, _, _, _ = context("stopped", "playing")
    rt = ctx.runtime
    rt.media_context = ctx
    rt.movie_controller_provider = lambda: movie
    rt._voice_request_generation = 1
    stop = Mock()
    rt.command_bus.register(C.MOVIE_STOP, stop)
    control = NaturalControl(rt, InteractionMemory(tmp_path / "habits.db"))
    result = control.execute(Command(C.NATURAL_MEDIA, extract("这个一般般").to_dict()))
    assert result.status is S.ACCEPTED
    stop.assert_not_called()
    assert movie.current() is not None
    assert control.memory.flush(timeout=None)
    assert list(control.memory.weak_profile("movie").values()) == [-0.1]
    assert control.close()


@pytest.mark.parametrize(
    "text,revoked",
    [("我不是让你换歌", True), ("我不是让你暂停音乐", False), ("别再跳过这首啦", False)],
)
def test_denied_previous_action_never_executes_and_only_undoes_attributable_feedback(
    controller, tmp_path, text, revoked
):
    ready(controller)
    rt, control = build(controller, tmp_path)
    assert natural(rt, "这首不好听").status is S.ACCEPTED
    settle(controller)
    assert control.memory.flush(timeout=None)
    before = control.memory.weak_profile("music")
    loads = len(controller.mpv.loaded)
    rt._execute_intent(rt.intent_router.parse(text), text)
    assert control.memory.flush(timeout=None)
    assert control.memory.weak_profile("music") == ({} if revoked else before)
    assert len(controller.mpv.loaded) == loads
    assert control.close()


@pytest.mark.parametrize("text,page", [("这个不好看", "music"), ("这个一般般", "movie")])
def test_movie_evaluation_never_skips_background_music_without_movie_target(
    controller, tmp_path, text, page
):
    ready(controller)
    rt, control = build(controller, tmp_path)
    rt.media_context.note_page(page)
    before = controller._play_request_id
    try:
        result = natural(rt, text)
        assert result.status is S.NEEDS_PARAMETER
        assert controller._play_request_id == before
        assert control.memory.flush(timeout=None)
        assert control.memory.weak_profile("music") == {}
    finally:
        assert control.close()


def test_explicit_song_evaluation_still_controls_music_on_movie_page(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    rt.media_context.note_page("movie")
    before = controller._play_request_id
    assert natural(rt, "这首歌一般般").status is S.ACCEPTED
    settle(controller)
    assert controller._play_request_id == before + 1
    assert control.close()


def test_page_evidence_changing_after_selection_blocks_old_feedback(controller, tmp_path):
    ready(controller)
    rt, control = build(controller, tmp_path)
    rt.media_context.note_page("music")
    freeze = control._frozen_song

    def changed(c):
        song = freeze(c)
        rt.media_context.note_page("movie")
        return song

    control._frozen_song = changed
    before = controller._play_request_id
    assert natural(rt, "这个一般般").status is S.UNKNOWN
    assert controller._play_request_id == before
    assert control.memory.flush(timeout=None) and control.memory.weak_profile("music") == {}
    assert control.close()
