"""
包2a 测试：Assrt 字幕链（搜索解析 / ZIP GBK 修复 / 假字幕过滤 / RAR tar 解压 / 直接 /
sub-add + sid 强制选中 / 失败降级）。全部 mock 网络与外部程序，不污染真实 data/。
"""
import pytest

pytestmark = pytest.mark.unit

import io
import os
import subprocess
import sys
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from unittest.mock import ANY, MagicMock, patch

from app.movie.movie_controller import MovieController


class FakeConfig:
    """测试配置 stub：控制字幕目录指向 tmp，避免写真实 data/。"""

    def __init__(self, tmp, token="TOKEN"):
        self._d = {
            "movie.source_url": "http://source.test/api",
            "movie.mpv_ipc_pipe": r"\\.\pipe\test_movie",
            "music.mpv_path": "mpv",
            "app.data_dir": str(tmp),
            "movie.assrt_token": token,
            "movie.auto_next": True,
            "movie.subtitle_dir": str(tmp / "subs"),
        }

    def get(self, key, default=None):
        return self._d.get(key, default)


def _make_ctrl(tmp, token="TOKEN"):
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(config=FakeConfig(tmp, token))
        ctrl.mpv = M.return_value
        return ctrl


# ---------- 字幕 Token / 搜索 ----------

def test_search_subtitle_no_token_returns_empty(tmp_path):
    ctrl = _make_ctrl(tmp_path, token="")
    assert ctrl.search_subtitle("流浪地球") == []


def test_search_subtitle_parses(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    body = ('{"status":0,"sub":{"subs":['
            '{"id":715932,"native_name":"流浪地球2 (2023)","videoname":"x",'
            '"lang":{"desc":"简 繁"},"subtype":"SSA","vote_score":100}]}}')
    with patch("app.movie.movie_controller.requests.get") as g:
        g.return_value = MagicMock(text=body)
        subs = ctrl.search_subtitle("流浪地球2")
    assert len(subs) == 1
    assert subs[0]["id"] == 715932
    assert subs[0]["native_name"] == "流浪地球2 (2023)"
    assert subs[0]["lang_desc"] == "简 繁"
    assert subs[0]["subtype"] == "SSA"
    assert subs[0]["vote_score"] == 100


def test_search_subtitle_status_error_and_exception(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    with patch("app.movie.movie_controller.requests.get") as g:
        g.return_value = MagicMock(text='{"status":20001,"errmsg":"invalid token"}')
        assert ctrl.search_subtitle("x") == []
    with patch("app.movie.movie_controller.requests.get", side_effect=Exception("boom")):
        assert ctrl.search_subtitle("x") == []


def test_search_subtitle_uses_proxy_and_token(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    body = '{"status":0,"sub":{"subs":[]}}'
    with patch("app.movie.movie_controller.requests.get") as g:
        g.return_value = MagicMock(text=body)
        ctrl.search_subtitle("流浪地球")
    kwargs = g.call_args.kwargs
    assert kwargs["proxies"] == {"http": "http://127.0.0.1:7897",
                                 "https": "http://127.0.0.1:7897"}
    assert kwargs["headers"]["Authorization"] == "Bearer TOKEN"
    assert kwargs["params"]["q"] == "流浪地球"


# ---------- 假字幕过滤 / ZIP ----------

def test_is_real_subtitle():
    ctrl = _make_ctrl(__import__("pathlib").Path("."))
    assert ctrl._is_real_subtitle(b"1\n00:00:01,000 --> 00:00:03,000\nhello") is True
    assert ctrl._is_real_subtitle(b"[Script Info]\nScriptType: v4.00+") is True
    assert ctrl._is_real_subtitle(b"") is False


def test_fake_subtitle_netdisk_link():
    ctrl = _make_ctrl(__import__("pathlib").Path("."))
    fake = ("通过网盘分享的文件\n链接: https://pan.baidu.com/s/abc \u63d0\u53d6\u7801: yurg"
            ).encode("utf-8")
    assert ctrl._is_real_subtitle(fake) is False


def test_zip_name_gbk_fix():
    """无 UTF-8 flag 的 GBK 文件名：cp437→gbk 修复（已实测坑）。"""
    ctrl = _make_ctrl(__import__("pathlib").Path("."))
    gbk_bytes = "我的字幕.srt".encode("gbk")
    cp437_str = gbk_bytes.decode("cp437")   # zipfile 按 cp437 读出的乱码
    assert cp437_str != "我的字幕.srt"
    assert ctrl._fix_zip_name(cp437_str, 0) == "我的字幕.srt"
    # UTF-8 flag 置位 → 原名
    assert ctrl._fix_zip_name("normal.srt", 0x800) == "normal.srt"


def _make_zip(files):
    """内存造 zip：files = [(name, content_bytes)]。ASCII 名不设 UTF-8 flag。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files:
            z.writestr(name, content)
    return buf.getvalue()


def test_extract_zip_real_srt(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    data = _make_zip([("sub.srt", b"1\n00:00:01,000 --> 00:00:02,000\nhi")])
    out = ctrl._extract_zip(data)
    assert len(out) == 1
    assert out[0][0] == "sub.srt"
    assert b"-->" in out[0][1]


def test_extract_zip_skips_netdisk_txt(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    fake = b"\xe9\x80\x9a\xe8\xbf\x87\xe7\xbd\x91\xe7\x9b\x98 pan.baidu.com/s/abc"
    data = _make_zip([("说明.txt", fake), ("real.srt", b"1\n00:00:01,000 --> x")])
    out = ctrl._extract_zip(data)
    assert [n for n, _ in out] == ["real.srt"]


def test_extract_zip_non_subtitle_ext_skipped(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    data = _make_zip([("readme.md", b"[Script Info]")])
    assert ctrl._extract_zip(data) == []


# ---------- RAR（tar） ----------

def test_extract_rar_uses_tar(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    rar = b"Rar!\x1a\x07\x01\x00fake"
    sub_dir = tmp_path / "sub_dir"
    sub_dir.mkdir()
    (sub_dir / "movie.ass").write_bytes(b"[Script Info]\nScriptType: v4.00+")
    with patch("app.movie.movie_controller.shutil.which", return_value=r"C:\Windows\System32\tar.exe"), \
         patch("app.movie.movie_controller.tempfile.mkdtemp", return_value=str(sub_dir)), \
         patch("app.movie.movie_controller.subprocess.run") as run:
        out = ctrl._extract_rar(rar)
    assert run.call_args.args[0] == ["C:\\Windows\\System32\\tar.exe", "-xf",
                                     ANY, "-C", str(sub_dir)]
    assert run.call_args.kwargs["timeout"] == 15
    assert [n for n, _ in out] == ["movie.ass"]


def test_extract_rar_no_tar(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    with patch("app.movie.movie_controller.shutil.which", return_value=None):
        assert ctrl._extract_rar(b"Rar!x") == []


def test_extract_rar_timeout(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    with patch("app.movie.movie_controller.shutil.which", return_value="tar"), \
         patch("app.movie.movie_controller.tempfile.mkdtemp",
               return_value=str(tmp_path / "t")), \
         patch("app.movie.movie_controller.subprocess.run",
               side_effect=subprocess.TimeoutExpired("tar", 15)):
        assert ctrl._extract_rar(b"Rar!x") == []


def test_extract_rar_filters_netdisk(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    sub_dir = tmp_path / "sub_dir"
    sub_dir.mkdir()
    (sub_dir / "bad.txt").write_bytes(b"pan.baidu.com link")
    (sub_dir / "good.srt").write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nok")
    with patch("app.movie.movie_controller.shutil.which", return_value="tar"), \
         patch("app.movie.movie_controller.tempfile.mkdtemp", return_value=str(sub_dir)), \
         patch("app.movie.movie_controller.subprocess.run"):
        out = ctrl._extract_rar(b"Rar!x")
    assert [n for n, _ in out] == ["good.srt"]


# ---------- 直接字幕 / load_subtitle 全链 ----------

def test_extract_direct_subtitle(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    assert ctrl._extract_subtitle(b"1\n00:00:01,000 --> 00:00:02,000\nhi",
                                  "sub.srt") == [("sub.srt", b"1\n00:00:01,000 --> 00:00:02,000\nhi")]


def test_load_subtitle_full_chain(tmp_path):
    """detail → 下载 ZIP → 解压 → 落盘 → sub_add → sid 强制选中。"""
    ctrl = _make_ctrl(tmp_path)
    ctrl.mpv.is_running.return_value = True
    ctrl.mpv.get_track_list.return_value = [
        {"id": 1, "type": "video"},
        {"id": 2, "type": "sub", "external": True, "selected": False},
    ]
    zip_data = _make_zip([("流浪地球2.srt", b"1\n00:00:01,000 --> 00:00:02,000\nhi")])
    detail_body = '{"status":0,"sub":{"subs":[{"url":"http://file0.assrt.net/dl/1.zip"}]}}'
    with patch("app.movie.movie_controller.requests.get") as g:
        g.side_effect = [
            MagicMock(text=detail_body),
            MagicMock(status_code=200, content=zip_data),
        ]
        ok, msg = ctrl.load_subtitle(715932)
    assert ok is True
    # 落盘 + 加载 + 选中
    assert ctrl.mpv.sub_add.called
    added = ctrl.mpv.sub_add.call_args.args[0]
    assert added.endswith(".srt")
    assert os.path.exists(added)
    ctrl.mpv.set_sid.assert_called_once_with(2)
    assert ctrl.subtitle_dir() == str(tmp_path / "subs")


def test_load_subtitle_no_token(tmp_path):
    ctrl = _make_ctrl(tmp_path, token="")
    ok, msg = ctrl.load_subtitle(1)
    assert ok is False
    assert "Token" in msg


def test_load_subtitle_detail_fail_and_download_fail(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    with patch("app.movie.movie_controller.requests.get") as g:
        g.side_effect = [
            MagicMock(text='{"status":20001}'),
        ]
        ok, msg = ctrl.load_subtitle(1)
    assert ok is False
    with patch("app.movie.movie_controller.requests.get") as g:
        g.side_effect = [
            MagicMock(text='{"status":0,"sub":{"subs":[{"url":"http://x/1.zip"}]}}'),
            MagicMock(status_code=403, content=b""),
        ]
        ok, msg = ctrl.load_subtitle(1)
    assert ok is False


def test_load_subtitle_netdisk_zip_rejected(tmp_path):
    """ZIP 内只有网盘链接说明 → 视为无有效字幕，返回失败不崩。"""
    ctrl = _make_ctrl(tmp_path)
    fake = b"\xe9\x93\xbe\xe6\x8e\xa5: https://pan.baidu.com/s/abc \xe6\x8f\x90\xe5\x8f\x96\xe7\xa0\x81: yurg"
    zip_data = _make_zip([("说明.txt", fake)])
    detail_body = '{"status":0,"sub":{"subs":[{"url":"http://x/1.zip"}]}}'
    with patch("app.movie.movie_controller.requests.get") as g:
        g.side_effect = [MagicMock(text=detail_body),
                         MagicMock(status_code=200, content=zip_data)]
        ok, msg = ctrl.load_subtitle(1)
    assert ok is False
    assert "无有效字幕" in msg


def test_maybe_mark_sub_searched(tmp_path):
    ctrl = _make_ctrl(tmp_path)
    it = {"vod_id": 42}
    assert ctrl.sub_searched(it) is False
    ctrl.maybe_mark_sub_searched(it)
    assert ctrl.sub_searched(it) is True
    assert ctrl.sub_searched({"vod_id": 99}) is False
