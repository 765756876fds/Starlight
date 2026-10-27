"""highlight_detector 统一入口测试：默认 V4 Early-Rise。"""
import os
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

import app.music.highlight_detector as hd


def test_detect_calls_v4_directly(tmp_path):
    """detect_highlight 直接走 V4，不调用 pychorus。"""
    p = tmp_path / "a.flac"
    p.write_bytes(b"\x00" * 10)
    with patch.object(hd, "_try_v4", return_value=(30.0, 60.0, 0.7)) as mock_v4, \
         patch.object(hd, "_try_pychorus") as mock_pc:
        r = hd.detect_highlight(str(p))
    mock_v4.assert_called_once()
    mock_pc.assert_not_called()
    assert r == (30.0, 60.0, 0.7)
    assert hd.pop_last_source() == hd.SOURCE_TAG_V4


def test_detect_v4_returns_none(tmp_path):
    p = tmp_path / "b.flac"
    p.write_bytes(b"\x00" * 10)
    with patch.object(hd, "_try_v4", return_value=None):
        assert hd.detect_highlight(str(p)) is None


def test_missing_file_returns_none():
    assert hd.detect_highlight("/nonexistent/path.flac") is None


def test_get_source_tag_returns_v4():
    assert hd.get_source_tag() == "local_v4_early_rise"


def test_algorithm_version_string():
    assert hd.get_algorithm_version() == "v4_early_rise"
