"""本地视频入口的无副作用回归测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.ui


def _panel_stub():
    from app.ui.panels.movie_panel import MoviePanel

    panel = MoviePanel.__new__(MoviePanel)
    panel.lbl_status = MagicMock()
    panel._movie = SimpleNamespace(is_playing=MagicMock(return_value=True))
    panel._play_item = MagicMock()
    return panel


def test_local_video_cancel_does_not_play():
    panel = _panel_stub()
    with patch("app.ui.panels.movie_panel.QFileDialog.getOpenFileName", return_value=("", "")):
        panel._open_local_video()
    panel._play_item.assert_not_called()
    panel.lbl_status.setText.assert_not_called()


def test_local_video_supports_unicode_and_spaces(tmp_path):
    path = tmp_path / "中文 电影.mp4"
    path.write_bytes(b"not-a-real-video")
    panel = _panel_stub()
    with patch("app.ui.panels.movie_panel.QFileDialog.getOpenFileName", return_value=(str(path), "")):
        panel._open_local_video()
    panel._play_item.assert_called_once()
    item = panel._play_item.call_args.args[0]
    assert item["first_url"] == str(path.resolve())
    assert item["local_file"] is True


def test_local_video_missing_file_reports_without_playing(tmp_path):
    panel = _panel_stub()
    missing = tmp_path / "missing.mp4"
    with patch("app.ui.panels.movie_panel.QFileDialog.getOpenFileName", return_value=(str(missing), "")):
        panel._open_local_video()
    panel._play_item.assert_not_called()
    panel.lbl_status.setText.assert_called_once_with("无法打开本地视频：文件不存在或不可访问")
