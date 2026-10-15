"""历史 tab 刷新：切到历史 tab（index=2）触发 _refresh_history。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeMusic:
    """面板所需的最小 controller 接口（含历史相关）。"""

    def get_history(self, limit=200):
        return [{"id": 1, "title": "T", "artist": "A", "source_type": "local",
                 "source_name": "", "music_id": 1, "played_at": 1000.0}]

    def get_play_mode(self):
        return "sequential"

    def is_highlight_mode(self):
        return False

    def get_volume(self):
        return 70

    def get_online_sources(self):
        return []

    def search_local(self, kw=""):
        return []

    def get_current_song(self):
        return None

    def clear_history(self):
        pass

    def play_history_item(self, item):
        return True


def test_switch_to_history_tab_refreshes(qapp):
    """切到历史 tab → _refresh_history 重新查询历史（构造时 1 次 + 切换 1 次）。"""
    from app.ui.panels.music_panel import MusicPanel
    music = FakeMusic()
    with patch.object(music, "get_history", wraps=music.get_history) as gh:
        panel = MusicPanel(music=music)
        assert gh.call_count >= 1  # 构造时已刷
        before = gh.call_count
        panel.tabs.setCurrentIndex(2)  # 切到历史
        assert gh.call_count == before + 1


def test_switch_to_non_history_tab_no_refresh(qapp):
    """切到本地/在线 tab → 不触发历史刷新（避免无谓查库）。"""
    from app.ui.panels.music_panel import MusicPanel
    music = FakeMusic()
    with patch.object(music, "get_history", wraps=music.get_history) as gh:
        panel = MusicPanel(music=music)
        before = gh.call_count
        panel.tabs.setCurrentIndex(0)  # 本地
        panel.tabs.setCurrentIndex(1)  # 在线
        assert gh.call_count == before
