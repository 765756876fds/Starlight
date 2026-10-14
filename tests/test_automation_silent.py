"""修 3（P2）：automation 关闭后不再刷屏。

覆盖：
- 四功能全关时 _tick 直接 return：不打印、不做任何 poll 请求
- ban 开时打 champ_select poll 日志
- ban/pick 全关（queue 开）时不打 champ_select 日志（仍保留 poll 调用，404 双保险静默）
"""
from __future__ import annotations

import pytest

from app.lol.automation.models import AutomationConfig
from app.lol.automation.service import LoLAutomationService


class _FakeLCU:
    connected = True

    def __init__(self):
        self.phase = "ChampSelect"
        self.calls = []

    def get_gameflow_phase(self):
        return self.phase

    def request_raw(self, method, endpoint, data=None):
        self.calls.append((method, endpoint))
        # ChampSelect 相关 GET 一律 404：ban/pick 门控 + 404 双保险路径
        if endpoint == "/lol-summoner/v1/current-summoner":
            return 200, {"summonerId": 1}
        return 404, None


class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, event, data=None):
        self.events.append((event, data))


def _mk_svc(cfg_kwargs: dict) -> LoLAutomationService:
    cfg = AutomationConfig(enabled=True, **cfg_kwargs)
    bus = _FakeBus()
    return LoLAutomationService(lcu=_FakeLCU(), config=cfg, event_bus=bus)


def test_all_off_tick_silent(capsys):
    """四功能全关：_tick 不打印、不发任何请求。"""
    svc = _mk_svc({})
    svc._tick()
    out = capsys.readouterr().out
    assert "[champ_select]" not in out
    assert svc._lcu.calls == []              # 无网络请求（poll 到不了）


def test_ban_on_logs_champ_select(capsys):
    """ban 开 → 打 champ_select poll 日志。"""
    svc = _mk_svc({"auto_ban": True})
    svc._tick()
    out = capsys.readouterr().out
    assert "[champ_select]" in out


def test_queue_only_no_champ_select_log(capsys):
    """queue 开但 ban/pick 全关 → 不打 champ_select 日志（poll 静默调用）。"""
    svc = _mk_svc({"auto_queue": True})
    svc._tick()
    out = capsys.readouterr().out
    assert "[champ_select]" not in out
