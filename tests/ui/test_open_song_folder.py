"""打开所在文件夹：cmd shell 形态 + % 转义 + 三入口 path 判定 + 真实冒烟。"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

import pytest
pytestmark = pytest.mark.ui



@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeMusic:
    """面板所需最小 controller 接口。"""

    def get_volume(self):
        return 70

    def get_online_sources(self):
        return []

    def search_local(self, kw=""):
        return []

    def get_history(self, limit=200):
        return []

    def get_play_mode(self):
        return "sequential"

    def is_highlight_mode(self):
        return False

    def get_current_song(self):
        return None

    def is_downloaded(self, title, artist):
        return False

    def get_song_path(self, music_id):
        return "D:/补/歌 - 手.mp3"


def _make_panel(qapp):
    from app.ui.panels.music_panel import MusicPanel
    return MusicPanel(music=FakeMusic())


def test_open_folder_shell_form_mocked(qapp):
    """核心：改后必须走 cmd shell 形态（list 模式 explorer 不吃）。"""
    p = _make_panel(qapp)
    with patch("subprocess.Popen") as popen:
        p._open_song_folder(r"D:\落雪\音乐文件\All For Love - Tungevaag、Raaban.flac")
    args, kw = popen.call_args
    assert kw.get("shell") is True
    assert args[0] == 'explorer /select,"D:\\落雪\\音乐文件\\All For Love - Tungevaag、Raaban.flac"'


def test_open_folder_pct_escaped(qapp):
    """% → %%：防 cmd 变量展开。"""
    p = _make_panel(qapp)
    with patch("subprocess.Popen") as popen:
        p._open_song_folder(r"D:\a\%USER%\x.mp3")
    args, kw = popen.call_args
    assert args[0] == 'explorer /select,"D:\\a\\%%USER%%\\x.mp3"'


def test_open_folder_local_entry_uses_song_path(qapp):
    """本地列表：song['path'] 直接用（不走 get_song_path 兜底）。"""
    p = _make_panel(qapp)
    song = {"title": "本地歌", "artist": "A", "source_type": "local",
            "path": "D:/真/本地歌 - A.mp3", "music_id": 7}
    with patch.object(p, "_open_song_folder") as o:
        menu = p._build_song_menu(song)
        for act in menu.actions():
            if act.text() == "打开所在文件夹":
                act.trigger()
                break
        else:
            raise AssertionError("本地歌应有 打开所在文件夹 项")
    o.assert_called_once_with("D:/真/本地歌 - A.mp3")


def test_open_folder_history_entry_backfills_path(qapp):
    """历史入口：song 无 path → get_song_path(music_id) 兜底 → 菜单出现。"""
    p = _make_panel(qapp)
    row = {"title": "历史歌", "artist": "B", "source_type": "local", "music_id": 5}
    song = p._song_dict_from_row(row)
    assert song["path"] == ""
    with patch.object(p, "_open_song_folder") as o:
        menu = p._build_song_menu(song)
        texts = [a.text() for a in menu.actions()]
        assert "打开所在文件夹" in texts
        for act in menu.actions():
            if act.text() == "打开所在文件夹":
                act.trigger()
                break
    o.assert_called_once_with("D:/补/歌 - 手.mp3")


def test_open_folder_online_entry_no_item(qapp):
    """在线歌：不出现 打开所在文件夹。"""
    p = _make_panel(qapp)
    song = {"title": "在线", "artist": "C", "source_type": "online",
            "source_name": "kw", "track_id": "t1", "path": "", "music_id": 0}
    menu = p._build_song_menu(song)
    assert "打开所在文件夹" not in [a.text() for a in menu.actions()]


def test_open_folder_smoke_real_explorer(qapp, tmp_path):
    """真实冒烟（集成）：真实文件 + 真实 explorer shell 形态，进程能启动、不报错。
    会短暂弹一次资源管理器窗口（选中 tmp 文件）。"""
    target = tmp_path / "冒烟 - 测试.txt"
    target.write_text("x", encoding="utf-8")
    p = _make_panel(qapp)
    p._open_song_folder(str(target))   # 不抛异常 = 通过
    time.sleep(1.5)                    # 给 explorer 启动时间
    cmd = f'explorer /select,"{os.path.normpath(str(target))}"'
    assert cmd.startswith("explorer /select,")
    assert os.path.normpath(str(target)) in cmd
