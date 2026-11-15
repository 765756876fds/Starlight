"""Finite natural controls -> real live-state route -> real isolated MPV IPC."""
from unittest.mock import Mock

from app.core.command_bus import CommandBus, CommandType
from app.core.operation_result import OperationStatus
from app.voice.runtime import VoiceRuntime
from tests.integration.test_music_eof_mpv import native as native, wait


def test_natural_controls_pause_and_resume_real_generated_audio(native):
    s = native
    s.ctrl.queue.add_all(s.songs)
    s.ctrl.next()
    wait(lambda: s.mpv.get_property("time-pos") is not None)
    rt = VoiceRuntime({})
    rt.command_bus = CommandBus()
    rt.command_bus.register(CommandType.MUSIC_PAUSE, lambda command: s.ctrl.pause())
    rt.command_bus.register(CommandType.MUSIC_RESUME, lambda command: s.ctrl.resume())
    rt.music_mpv_provider = lambda: s.mpv
    rt.music_controller_provider = lambda: s.ctrl
    rt._enqueue_speak = Mock()
    for pause, resume in (("停一下", "继续播放"), ("先停一下", "接着放")):
        for text, paused in ((pause, True), (resume, False)):
            before = len(s.mpv.loads)
            result = rt.media_context.execute(rt.intent_router.parse(text).params["action"])
            assert result.status == OperationStatus.CONFIRMED
            assert s.mpv.get_property("pause") is paused
            assert len(s.mpv.loads) == before, "controls must not replace the current track"
