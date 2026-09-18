"""OverlayModel 单元测试（不依赖 Qt event loop）。"""
import time

from app.lol.cooldown.models import CooldownReminder
from app.ui.overlay.overlay_model import OverlayModel


def _make_reminder(champ="亚索", skill="闪现", side="enemy",
                   remaining=300.0):
    now = time.monotonic()
    return CooldownReminder(
        id="r1", champion_id=157, champion_name=champ,
        skill=skill, skill_type="summoner_spell",
        base_cooldown=300.0, estimated_cooldown=300.0,
        used_at=now + remaining - 300.0,
        remind_before=0, side=side,
    )


class FakeCd:
    def __init__(self, reminders):
        self._r = reminders
    def list_active(self):
        return list(self._r)


def test_empty_when_no_data():
    m = OverlayModel(cooldown_manager=FakeCd([]), phase_provider=lambda: "InProgress")
    assert m.get_items() == []


def test_items_format():
    r = _make_reminder()
    m = OverlayModel(cooldown_manager=FakeCd([r]), phase_provider=lambda: "InProgress")
    items = m.get_items()
    assert len(items) == 1
    assert "亚索" in items[0].text
    assert "闪现" in items[0].text
    assert "敌方" in items[0].text
    assert items[0].seconds_remaining > 290


def test_urgent_flag():
    r = _make_reminder(remaining=15.0)
    m = OverlayModel(cooldown_manager=FakeCd([r]), phase_provider=lambda: "InProgress")
    items = m.get_items()
    assert items[0].urgent is True


def test_not_urgent_when_far():
    r = _make_reminder(remaining=120.0)
    m = OverlayModel(cooldown_manager=FakeCd([r]), phase_provider=lambda: "InProgress")
    items = m.get_items()
    assert items[0].urgent is False


def test_disabled_hides_items():
    r = _make_reminder()
    m = OverlayModel(cooldown_manager=FakeCd([r]), phase_provider=lambda: "InProgress")
    m.set_enabled(False)
    assert m.get_items() == []


def test_max_items():
    rs = [_make_reminder(remaining=float(i * 10)) for i in range(20)]
    m = OverlayModel(cooldown_manager=FakeCd(rs), max_items=8, phase_provider=lambda: "InProgress")
    items = m.get_items()
    assert len(items) == 8


def test_no_cooldown_manager():
    m = OverlayModel(cooldown_manager=None, phase_provider=lambda: "InProgress")
    assert m.get_items() == []


def test_filters_by_phase():
    r = _make_reminder()
    cd = FakeCd([r])
    for phase in ["None", "Lobby", "Matchmaking", "ReadyCheck"]:
        m = OverlayModel(cooldown_manager=cd, phase_provider=lambda p=phase: p)
        assert m.get_items() == [], f"phase={phase} should hide"
    for phase in ["InProgress", "ChampSelect"]:
        m = OverlayModel(cooldown_manager=cd, phase_provider=lambda p=phase: p)
        assert len(m.get_items()) == 1, f"phase={phase} should show"


class FakeObj:
    def __init__(self, timers):
        self._t = timers
    def get_next_timers(self):
        return list(self._t)


def test_includes_objectives():
    obj = FakeObj([
        {"name": "小龙", "seconds_remaining": 123.0},
        {"name": "大龙", "seconds_remaining": 245.0},
    ])
    m = OverlayModel(cooldown_manager=FakeCd([]), objective_timer=obj,
                     phase_provider=lambda: "InProgress")
    items = m.get_items()
    assert any("小龙" in i.text for i in items)
    assert any("大龙" in i.text for i in items)
