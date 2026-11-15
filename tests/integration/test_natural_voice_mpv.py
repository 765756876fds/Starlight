"""Production semantic adapter with generated WAV and real silent MPV IPC."""

from app.core.command_bus import Command, CommandType as C
from app.core.operation_result import OperationStatus as S
from app.music.preferences import media_key
from app.voice.interaction_memory import InteractionMemory
from app.voice.runtime import VoiceRuntime
from tests.integration.test_music_eof_mpv import native as native, wait


def test_evaluation_next_and_save_use_actual_mpv_and_sqlite(native, tmp_path):
    s = native
    s.ctrl.queue.add_all(s.songs)
    s.ctrl.next()
    wait(
        lambda: s.mpv.get_property("time-pos") is not None and s.mpv.get_property("time-pos") > 0.35
    )
    runtime = VoiceRuntime({})
    runtime.command_bus = s.ctrl.command_bus
    runtime.music_controller_provider = lambda: s.ctrl
    runtime.music_mpv_provider = lambda: s.mpv
    control = runtime.configure_natural_media(InteractionMemory(tmp_path / "private-habits.db"))
    try:
        first = dict(s.ctrl.get_current_song())
        for text in ("这首不错，给我留着", "这首不好听"):
            parsed = runtime.intent_router.parse(text)
            assert parsed.intent_type == C.NATURAL_MEDIA
            result = runtime.command_bus.execute(Command(parsed.intent_type, parsed.params))
            assert result.status in (S.CONFIRMED, S.ACCEPTED)
        wait(lambda: s.ctrl.get_current_song()["id"] == s.songs[1]["id"] and len(s.mpv.loads) == 2)
        assert control.memory.flush()
        assert control.memory.weak_profile("music") == {media_key(first): -0.3}
        playlist = s.ctrl.get_playlists()[0]
        assert playlist["name"] == "我喜欢的音乐"
        assert s.ctrl.db.get_playlist_songs(playlist["id"])[0]["music_id"] == first["id"]
        assert s.ctrl.queue.size() == 2
    finally:
        assert control.close()
