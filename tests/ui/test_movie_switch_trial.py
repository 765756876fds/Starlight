"""The offer appears only after current evidence and explicit confirmation gates."""
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
import pytest

from tests.ui.test_movie_voice_window import scene as scene
from tests.unit.test_movie_safe_switch import item

pytestmark = pytest.mark.ui


class Pool(QObject):
    returned = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.active = {}
        self.start = Mock(return_value=True)
        self.cancel = Mock()


@pytest.mark.parametrize("choice", ["continue", "confirm", "expired", "generation_changed", "recovered", "source_changed"])
def test_prompt_requires_verified_better_candidate_and_is_single_use(scene, choice):
    window, controller = scene.window, scene.controller
    trial = window.switch_trial
    assert not trial.enabled
    trial.enabled = True
    original, alternative = item(), item('second')
    controller._current = original
    controller._current_index = 0
    controller._play_attempt = 1
    controller.mpv._process = SimpleNamespace(poll=lambda: None)
    revision = [0]
    manager = SimpleNamespace(get_search_revision=lambda: revision[0], get_enabled_sources=lambda: [])
    pool = Pool()
    trial.prepare(SimpleNamespace(pool=pool, request=None, manager=manager), [alternative])
    controller.switch_verified_source = Mock(return_value="rejected")
    controller.source_quality_snapshot = Mock(return_value=dict(current=SimpleNamespace(
        state="OBSERVING", sustained_transport_wait=False)))
    window.show()
    window._playing = True
    controller.set_source_quality_trial(True)
    window._poll_quality_notice()
    assert not window.quality_notice.isVisible()
    pool.start.assert_not_called()
    controller.source_quality_snapshot.return_value['current'] = SimpleNamespace(
        state="BUFFERING", sustained_transport_wait=True)
    window._poll_quality_notice()
    pool.start.assert_called_once()
    assert not window.quality_notice.isVisible()
    token = trial.token
    pool.returned.emit(token, dict(status='ok', duration=20))
    window._poll_quality_notice()
    assert window.quality_notice.isVisible()
    assert window.quality_notice.view_button.text() == "换源"
    assert window.quality_notice.close_button.text() == "继续观看"
    controller.switch_verified_source.assert_not_called()
    if choice == "continue":
        window.quality_notice.close_button.click()
    elif choice == "expired":
        trial.expires = 0
        window.quality_notice.view_button.click()
    elif choice == "generation_changed":
        controller._playback_gen += 1
        window.quality_notice.view_button.click()
    elif choice == "source_changed":
        revision[0] += 1
        window.quality_notice.view_button.click()
    elif choice == "recovered":
        controller.source_quality_snapshot.return_value['current'].state = 'STABLE'
        window.quality_notice.view_button.click()
    else:
        window.quality_notice.view_button.click()
        for _ in range(50):
            if controller.switch_verified_source.called:
                break
            QTest.qWait(10)
        controller.switch_verified_source.assert_called_once()
    window._poll_quality_notice()
    assert not window.quality_notice.isVisible()
    assert controller.switch_verified_source.call_count == int(choice == "confirm")
    pool.start.assert_called_once()


def test_missing_match_is_counted_once_and_only_during_a_stall(scene, caplog):
    window, controller = scene.window, scene.controller
    controller._current = item()
    controller._current_index = 0
    controller.mpv._process = SimpleNamespace(poll=lambda: None)
    trial = window.switch_trial
    trial.enabled = True
    pool = Pool()
    trial.prepare(SimpleNamespace(pool=pool, request=None, manager=SimpleNamespace(
        get_search_revision=lambda: 1, get_enabled_sources=lambda: [])), [])
    controller.source_quality_snapshot = Mock(return_value={"current": SimpleNamespace(
        state="STABLE", sustained_transport_wait=False)})
    assert not trial.poll(True)
    assert not trial.reason_counts

    controller.source_quality_snapshot.return_value["current"] = SimpleNamespace(
        state="BUFFERING", sustained_transport_wait=True)
    with caplog.at_level("INFO", logger="app.ui.widgets.movie_switch_trial"):
        for _ in range(20):
            assert not trial.poll(True)
    assert dict(trial.reason_counts) == {"no_candidate": 1}
    assert [record.getMessage() for record in caplog.records] == ["movie_switch_check result=no_candidate count=1"]
    pool.start.assert_not_called()


def test_close_during_native_poll_defers_stop_and_invalidates_poll(scene):
    import threading
    from app.ui.widgets.latest_task import LatestTask

    window, controller = scene.window, scene.controller
    started, release = threading.Event(), threading.Event()
    observed = []

    def poll(current):
        started.set()
        if release.wait(2):
            observed.append(current())
        return None

    window._native_poll = LatestTask(window)
    window._native_poll.completed.connect(window._apply_film_poll)
    window._playing = True
    controller.poll_film_ui = poll
    controller.stop = Mock()
    try:
        window._tick()
        assert started.wait(.5)
        window.close()
        assert window._close_after_poll
        controller.stop.assert_not_called()
        release.set()
        for _ in range(50):
            if controller.stop.called:
                break
            QTest.qWait(10)
        assert observed == [False]
        controller.stop.assert_called_once()
        assert not window._playing
    finally:
        release.set()

def test_removed_cms_record_cannot_start_a_same_record_probe(scene):
    from app.movie.cms_parser import normalise_playback
    from app.movie.progressive_search import source_identity

    window, controller = scene.window, scene.controller
    cms = "https://removed.example/api"
    row = normalise_playback(dict(item(), _source_id=source_identity(cms),
                                  vod_play_url="正片$https://a.example/a.m3u8$$$正片$https://b.example/b.m3u8"))
    controller._current = row
    controller._current_index = 0
    controller._play_attempt = 1
    controller.mpv._process = SimpleNamespace(poll=lambda: None)
    controller.source_quality_snapshot = Mock(return_value=dict(current=SimpleNamespace(
        state="BUFFERING", sustained_transport_wait=True)))
    manager = SimpleNamespace(get_search_revision=lambda: 1,
                              get_enabled_sources=lambda: [{"url": "https://other.example/api"}])
    pool = Pool()
    trial = window.switch_trial
    trial.enabled = True
    trial.prepare(SimpleNamespace(pool=pool, request=None, manager=manager), [])
    assert trial.context is not None
    assert not trial.poll(True) and not trial.poll(True)
    assert trial.reason_counts["source_unavailable"] == 1
    pool.start.assert_not_called()
