"""P0 路径自适应测试（2026-10-16）：llama-server 查找链 + TTS mpv 解析。
覆盖：配置优先 / PATH / glob 兜底 / 全空降级 / mpv 默认一致 /
legacy 迁移保护（仅默认值）/ 显式配置不自动替换。"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.path_resolve import find_llama_server  # noqa: E402


# ---------- find_llama_server ----------

def test_find_llama_config_first(tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_text("x")
    with mock.patch("app.core.path_resolve.shutil.which", return_value=None) as mw, \
         mock.patch("app.core.path_resolve.glob.glob", return_value=[]) as mg:
        got = find_llama_server({"llama_server": str(exe)})
    assert got == str(exe)
    mw.assert_not_called()
    mg.assert_not_called()


def test_find_llama_config_missing_then_which():
    with mock.patch("app.core.path_resolve.shutil.which",
                    return_value="/usr/bin/llama-server"), \
         mock.patch("app.core.path_resolve.glob.glob", return_value=[]) as mg:
        got = find_llama_server({"llama_server": "C:/nope/llama-server.exe"})
    assert got == "/usr/bin/llama-server"
    mg.assert_not_called()


def test_find_llama_which():
    with mock.patch("app.core.path_resolve.shutil.which",
                    return_value="C:/tools/llama-server.exe") as m:
        got = find_llama_server(None)
    assert got == "C:/tools/llama-server.exe"
    m.assert_called_once_with("llama-server")


def test_find_llama_glob_latest(tmp_path):
    b1 = tmp_path / "backends" / "llama.cpp-a-1.0"; b1.mkdir(parents=True)
    b2 = tmp_path / "backends" / "llama.cpp-b-2.0"; b2.mkdir(parents=True)
    e1 = b1 / "llama-server.exe"; e1.write_text("x")
    e2 = b2 / "llama-server.exe"; e2.write_text("y")
    import time
    t = time.time()
    os.utime(str(e2), (t + 10, t + 10))        # e2 更新 → 应选中
    with mock.patch("app.core.path_resolve.shutil.which", return_value=None), \
         mock.patch("app.core.path_resolve.glob.glob",
                    return_value=[str(e1), str(e2)]):
        got = find_llama_server({})
    assert got == str(e2)


def test_find_llama_none_stays_degraded():
    with mock.patch("app.core.path_resolve.shutil.which", return_value=None), \
         mock.patch("app.core.path_resolve.glob.glob", return_value=[]):
        assert find_llama_server({}) == ""


# ---------- TTS mpv ----------

def _make_tts(tmp_path):
    from app.speech.tts import TTSController
    # bundled_tool 置空：隔离本机 third_party/mpv（新查找链 bundled 优先），
    # 保持"无配置 → 回退 'mpv'"的语义测试确定性。
    with mock.patch("app.speech.tts.get_event_bus"), \
         mock.patch("app.core.config.bundled_tool", return_value=""):
        return TTSController({
            "engine": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%", "volume": "+0%", "output_dir": str(tmp_path),
        })


def test_tts_default_mpv_consistent(tmp_path):
    tts = _make_tts(tmp_path)
    assert tts.mpv_path == "mpv"                # 与 music 链 controller.py:135 一致


def test_tts_resolve_config_isfile(tmp_path):
    exe = tmp_path / "mpv.exe"; exe.write_text("x")
    tts = _make_tts(tmp_path)
    tts.mpv_path = str(exe)
    assert tts._resolve_mpv_path() is True
    assert tts.mpv_path == str(exe)


def test_tts_resolve_which(tmp_path):
    tts = _make_tts(tmp_path)                   # mpv_path == "mpv"
    with mock.patch("os.path.isfile",
                    side_effect=lambda p: p != "mpv"), \
         mock.patch("shutil.which", return_value="C:/real/mpv.exe"):
        assert tts._resolve_mpv_path() is True
    assert tts.mpv_path == "C:/real/mpv.exe"


def test_tts_resolve_default_no_path_no_legacy(tmp_path):
    """P1-3（2026-10-18）：legacy 兜底已删——默认值且 PATH 无 → 直接失败（不查个人路径）。"""
    tts = _make_tts(tmp_path)
    with mock.patch("os.path.isfile", return_value=False), \
         mock.patch("shutil.which", return_value=None), \
         mock.patch("builtins.print"):
        assert tts._resolve_mpv_path() is False
    assert tts.mpv_path == "mpv"


def test_tts_resolve_explicit_config_never_replaced(tmp_path):
    tts = _make_tts(tmp_path)
    tts.mpv_path = "C:/nope/mpv.exe"            # 显式配置坏路径
    with mock.patch("os.path.isfile", return_value=False), \
         mock.patch("shutil.which", return_value=None), \
         mock.patch("builtins.print") as mp:
        assert tts._resolve_mpv_path() is False
    assert tts.mpv_path == "C:/nope/mpv.exe"
    mp.assert_called_once()                     # 降级提示保留
