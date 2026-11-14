"""Real live-state arbitration and existing CommandBus handlers, no Qt or audio devices."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.command_bus import CommandBus, CommandType as C
from app.core.operation_result import OperationStatus as S
from app.voice.media_context import MediaContext
from app.voice.runtime import VoiceRuntime


class Player:
    def __init__(self, domain, state):
        self.state, self.path = state, domain + ".mp4"
    def is_running(self):
        return self.state != "stopped"
    def get_property(self, key, **kwargs):
        if self.state == "unknown":
            return None
        return {"idle-active": self.state == "stopped", "pause": self.state == "paused", "path": self.path}[key]


def context(music="playing", movie="stopped"):
    players = {d: Player(d, s) for d, s in (("music", music), ("movie", movie))}
    controllers = {d: SimpleNamespace(_playback_gen=1, _play_request_id=1,
                                      queue=SimpleNamespace(size=lambda: 3)) for d in players}
    bus = CommandBus()
    runtime = SimpleNamespace(command_bus=bus)
    calls = []
    for domain in players:
        setattr(runtime, domain + "_mpv_provider", lambda d=domain: players[d])
        setattr(runtime, domain + "_controller_provider", lambda d=domain: controllers[d])
        for action, command in (("pause", C.MUSIC_PAUSE if domain == "music" else C.MOVIE_PAUSE),
                                ("resume", C.MUSIC_RESUME if domain == "music" else C.MOVIE_RESUME)):
            def handle(command, d=domain, a=action):
                calls.append((d, a))
                players[d].state = "paused" if a == "pause" else "playing"
                return True
            bus.register(command, handle)
    bus.register(C.MUSIC_NEXT, lambda command: calls.append(("music", "next")))
    return MediaContext(runtime), players, controllers, calls


@pytest.mark.parametrize("domain", ["music", "movie"])
def test_unique_live_player_pause_and_resume(domain):
    s, players, _, calls = context(**{domain: "playing", "movie" if domain == "music" else "music": "stopped"})
    assert s.execute("pause").status is S.CONFIRMED
    assert players[domain].state == "paused"
    assert s.execute("resume").status is S.CONFIRMED
    assert calls == [(domain, "pause"), (domain, "resume")]


def test_both_live_uses_current_focus_or_asks_without_guessing():
    s, _, _, calls = context("playing", "playing")
    assert s.execute("pause").status is S.NEEDS_PARAMETER
    assert calls == []
    s.note_focus("movie")
    assert s.execute("pause").status is S.CONFIRMED
    assert calls == [("movie", "pause")]


def test_recent_confirmed_pause_disambiguates_two_paused_players():
    s, players, _, calls = context("playing", "paused")
    assert s.execute("pause").status is S.CONFIRMED
    assert s.execute("resume").status is S.CONFIRMED
    assert calls == [("music", "pause"), ("music", "resume")]
    assert players["movie"].state == "paused"


def test_focus_and_pause_memory_cannot_survive_media_generation_change():
    s, players, controllers, calls = context("playing", "playing")
    s.note_focus("movie")
    controllers["movie"]._playback_gen += 1
    assert s.execute("pause").status is S.NEEDS_PARAMETER
    assert not calls
    players["music"].state = "paused"
    players["movie"].state = "paused"
    assert s.execute("resume").status is S.NEEDS_PARAMETER


@pytest.mark.parametrize("states", [("stopped", "stopped"), ("unknown", "stopped"), ("playing", "unknown")])
def test_no_reliable_target_never_executes(states):
    s, _, _, calls = context(*states)
    assert s.execute("pause").status is S.NEEDS_PARAMETER
    assert not calls


def test_stale_request_is_rejected_before_control():
    s, _, _, calls = context()
    assert s.execute("pause", is_current=lambda: False).status is S.UNKNOWN
    assert not calls


def test_other_player_starting_during_probe_invalidates_old_target():
    s, players, _, calls = context("playing", "stopped")
    sample = s.sample
    changed = []
    def racing(domain):
        result = sample(domain)
        if domain == "movie" and not changed:
            changed.append(True)
            players["movie"].state = "playing"
        return result
    s.sample = racing
    assert s.execute("pause").status is S.UNKNOWN
    assert calls == []


def test_next_uses_existing_music_queue_but_never_guesses_movie_episode():
    s, _, _, calls = context()
    assert s.execute("next").status is S.ACCEPTED
    assert calls == [("music", "next")]
    s, _, _, calls = context("stopped", "playing")
    assert s.execute("next").status is S.NEEDS_PARAMETER
    assert not calls


def test_expired_focus_and_unconfirmed_handler_cannot_report_success():
    s, _, _, calls = context("playing", "playing")
    now = [0]
    s.clock = lambda: now[0]
    s.note_focus("music")
    now[0] = 31
    assert s.execute("pause").status is S.NEEDS_PARAMETER
    s.note_focus("music")
    s.runtime.command_bus.register(C.MUSIC_PAUSE, lambda command: None)
    assert s.execute("pause").status is S.UNKNOWN
    assert not calls


@pytest.mark.parametrize("text,music,movie,expected", [
    ("先停一下", "playing", "stopped", [("music", "pause")]),
    ("停一会儿", "stopped", "playing", [("movie", "pause")]),
    ("继续", "stopped", "paused", [("movie", "resume")]),
    ("换一个", "playing", "stopped", [("music", "next")]),
    ("换一个", "paused", "stopped", [("music", "next")]),
    ("换一个", "stopped", "playing", []),
])
def test_actual_runtime_routes_context_without_new_recommendation(text, music, movie, expected):
    source, _, _, calls = context(music, movie)
    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    runtime.command_bus = source.runtime.command_bus
    for domain in ("music", "movie"):
        for suffix in ("_mpv_provider", "_controller_provider"):
            setattr(runtime, domain + suffix, getattr(source.runtime, domain + suffix))
    runtime._enqueue_speak = Mock()
    threads = []
    execute = runtime.command_bus.execute_async
    def track(command, on_done=None):
        thread = execute(command, on_done)
        threads.append(thread)
        return thread
    runtime.command_bus.execute_async = track
    runtime._execute_intent(runtime.intent_router.parse(text), text)
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert calls == expected
    assert not any(c.command_type == C.MUSIC_RECOMMEND for c in runtime.command_bus.get_history())
    runtime._enqueue_speak.assert_not_called()
