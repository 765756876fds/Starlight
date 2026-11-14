"""Generated video, real isolated MPV and SQLite. Null output is not a rendering test."""
import os
import time

import pytest

from app.core.command_bus import CommandBus, CommandType as C
from app.movie.preferences import FilmMemory
from app.voice.runtime import VoiceRuntime
from tests.integration.test_movie_resume_mpv import media as media, sessions as sessions, qapp as qapp

pytestmark = pytest.mark.integration


def wait(predicate, seconds=6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.03)
    assert predicate()


def same_path(value, expected):
    return isinstance(value, str) and os.path.normcase(os.path.abspath(value)) == os.path.normcase(str(expected.resolve()))


def test_real_voice_context_and_series_resume_in_new_process(sessions, media):
    s = sessions("first")
    s.mpv.extra_video_args.append("--no-resume-playback")
    item = dict(vod_name="生成剧集测试", type_name="电视剧", first_url=str(media[1]),
                play_url=f"第一集${media[1]}#第二集${media[2]}")
    assert s.ctl.play(item, index=1)
    wait(lambda: same_path(s.mpv.get_property("path", timeout=.2), media[2])
         and type(s.mpv.get_property("time-pos", timeout=.2)) in (int, float))
    s.seek(12)
    s.ctl.tick()
    memory_path = s.ctl.film_memory.path
    row = s.ctl.film_memory.records("tv")[0]
    assert row["position"] == pytest.approx(12, abs=.4) and row["episode"] == 1
    original_process = s.mpv._process.pid
    s.ctl.close()
    assert not s.mpv.is_running()

    fresh = sessions("second")
    fresh.mpv.extra_video_args.append("--no-resume-playback")
    fresh.ctl.film_memory = FilmMemory(memory_path)
    fresh.ctl.recommendations.memory = fresh.ctl.film_memory
    batch, current = fresh.ctl.recommendations.continuing("tv")
    assert current() and len(batch.rows) == 1
    saved = batch.rows[0]
    assert fresh.ctl.play(dict(saved["_resume_item"], _resume_position=saved["resume_seconds"]),
                          index=saved["_resume_episode"])
    wait(lambda: same_path(fresh.mpv.get_property("path", timeout=.2), media[2])
         and type(fresh.mpv.get_property("time-pos", timeout=.2)) in (int, float))
    assert fresh.mpv._process.pid != original_process
    assert fresh.mpv.get_property("time-pos", timeout=.2) < 5  # Native watch_later deliberately disabled.
    fresh.ctl.tick()
    wait(lambda: abs(fresh.mpv.get_property("time-pos", timeout=.2) - 12) < 2)

    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    runtime.command_bus = CommandBus()
    runtime.movie_mpv_provider = lambda: fresh.mpv
    runtime.movie_controller_provider = lambda: fresh.ctl
    runtime._enqueue_speak = lambda *a, **k: None
    runtime.command_bus.register(C.MOVIE_PAUSE, lambda command: fresh.ctl.pause())
    runtime.command_bus.register(C.MOVIE_RESUME, lambda command: fresh.ctl.resume())
    threads = []
    execute = runtime.command_bus.execute_async
    def track(command, on_done=None):
        thread = execute(command, on_done)
        threads.append(thread)
        return thread
    runtime.command_bus.execute_async = track
    runtime._execute_intent(runtime.intent_router.parse("先停一下"), "先停一下")
    wait(lambda: runtime.media_context._paused is not None)
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert fresh.mpv.get_property("pause", timeout=.2) is True
    runtime._execute_intent(runtime.intent_router.parse("接着放"), "接着放")
    wait(lambda: fresh.mpv.get_property("pause", timeout=.2) is False)
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert fresh.ctl.current()["play_url"] == item["play_url"]
