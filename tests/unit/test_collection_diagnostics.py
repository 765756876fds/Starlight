"""Observable queue ownership without changing state or emitting private metadata."""
import random

from app.core.operation_result import OperationStatus
from app.music.collection_diagnostics import record_collection
from app.music.queue import PlayMode
from tests.unit.test_music_recommendation import controller as controller


def lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if "music_collection " in line]


def test_real_recommendation_records_read_established_and_loaded_counts(controller, capsys):
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    messages = lines(capsys)
    assert any("stage=chart_read" in s and "track_count=10 retained_count=10" in s for s in messages)
    assert any("stage=established" in s and "queue_size=10 index=0" in s and "session_active=True" in s for s in messages)
    assert any("stage=loaded reason=recommendation" in s and "queue_size=10 index=0" in s for s in messages)
    controller.clear_queue()
    messages = lines(capsys)
    assert any("stage=session_cleared reason=clear_queue" in s for s in messages)
    assert any("stage=cleared reason=clear_queue" in s and "queue_size=0 index=-1" in s for s in messages)


def test_diagnostics_never_touch_shuffle_or_reveal_input(controller, capsys):
    secret = "https://fixture.invalid/private?token=never-log"
    controller.queue.add(dict(title=secret, artist="private-name", path=secret))
    controller.queue.set_play_mode(PlayMode.SHUFFLE)
    before = controller.queue.snapshot()
    rng = random.getstate()
    record_collection(controller, "established", "recommendation", source=secret, chart_id=secret,
                      track_count=1, retained_count=1)
    output = lines(capsys)
    assert controller.queue.snapshot() == before and random.getstate() == rng
    assert output and all(secret not in s and "private-name" not in s and "never-log" not in s for s in output)
    assert "source_ref=" in output[0] and "chart_ref=" in output[0]


def test_broken_diagnostic_sink_cannot_break_playback(controller, monkeypatch):
    def broken():
        raise RuntimeError("controlled diagnostic sink failure")
    monkeypatch.setattr("app.music.collection_diagnostics.get_logger", broken)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert controller.queue.size() == 10


def test_direct_and_list_replacement_are_distinguishable(controller, capsys):
    tracks = controller.online_manager.tracks
    assert controller.play_online(tracks[0]) is True
    assert any("stage=loaded reason=direct_online" in s and "queue_size=0" in s for s in lines(capsys))
    assert controller.play_online_list(tracks, 3) is True
    assert any("stage=replaced reason=online_list" in s and "track_count=10 retained_count=10" in s
               and "queue_size=10 index=3" in s for s in lines(capsys))
