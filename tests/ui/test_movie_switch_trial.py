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
