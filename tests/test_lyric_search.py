"""歌词搜歌测试：LyricIndex 构建/模糊匹配/标点清理 + 面板模式切换/懒构建/失效。"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from app.music.lyric_search import LyricIndex, _norm
from app.music.lyrics import parse_lrc

_id_counter = [0]


# =================================================================== 工具
def _write_song(tmp_path, name, lrc_text=None, lrc_encoding="utf-8"):
    """造一首歌：音频文件 + 同名 .lrc（无 lrc_text 则不写 .lrc）。"""
    _id_counter[0] += 1
    audio = tmp_path / f"{name}.mp3"
    audio.write_bytes(b"x")
    song = {"id": _id_counter[0], "path": str(audio),
            "title": name, "artist": "测试歌手", "album": "专辑"}
    if lrc_text is not None:
        if lrc_encoding == "utf-8":
            (tmp_path / f"{name}.lrc").write_text(lrc_text, encoding="utf-8")
        else:
            (tmp_path / f"{name}.lrc").write_bytes(lrc_text.encode("gbk"))
    return song


_LRC_QINGTIAN = """[ti:晴天]
[ar:周杰伦]
[00:12.00]故事的小黄花 从出生那年就飘着
[00:18.00]童年的荡秋千 随记忆一直晃到现在
[00:24.00]Re So So Si Do Si La So La Si Si Si Si La Si La So
[00:30.00]吹着前奏 望着天空的我想起花瓣试着掉落
[00:36.00]为你翘课的那一天 花落的那一天
[00:42.00]教室的那一间 我怎么看不见
[00:48.00]消失的下雨天 我好想再淋一遍
[00:54.00]没想到失去的勇气我还留着
[01:00.00]好想再问一遍 你会等待还是离开
[01:06.00]刮风这天 我试过握着你手
[01:12.00]但偏偏 雨渐渐 大到我看你不见
[01:18.00]还要多久 我才能在你身边
[01:24.00]等待放晴的那天 也许我会比较好一点
"""


# =================================================================== 索引构建
def test_build_extracts_lines(tmp_path):
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    n = idx.build([song])
    assert n > 0
    # 元标签 [ti:]/[ar:] 不收录；时间轴文本行收录
    texts = [line for _s, line in idx._entries]
    assert any("故事的小黄花" in t for t in texts)
    assert not any("ti:晴天" in t for t in texts)


def test_build_skips_no_lrc(tmp_path):
    song = _write_song(tmp_path, "无词歌")  # 无 .lrc
    idx = LyricIndex()
    assert idx.build([song]) == 0


def test_build_gbk_lrc(tmp_path):
    song = _write_song(tmp_path, "粤语歌",
                       "[00:01.00]海阔天空\n[00:02.00]你共我\n", lrc_encoding="gbk")
    idx = LyricIndex()
    assert idx.build([song]) == 2


def test_build_skips_broken_lrc(tmp_path):
    audio = tmp_path / "坏.mp3"
    audio.write_bytes(b"x")
    (tmp_path / "坏.lrc").write_bytes(b"\xff\xfe\x00garbage\x00")
    song = {"id": 1, "path": str(audio), "title": "坏", "artist": ""}
    idx = LyricIndex()
    n = idx.build([song])  # 解析失败 → 跳过不崩
    assert n == 0


def test_build_skips_meta_only_lrc(tmp_path):
    song = _write_song(tmp_path, "纯标签", "[ti:纯标签]\n[ar:某人]\n")
    idx = LyricIndex()
    assert idx.build([song]) == 0


def test_build_empty_and_nodb():
    idx = LyricIndex()
    assert idx.build([]) == 0
    assert idx.is_built() is True


# =================================================================== 搜索
def test_search_fuzzy_hit(tmp_path):
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    r = idx.search("小黄花")
    assert len(r) == 1 and r[0]["title"] == "晴天"


def test_search_punct_normalized(tmp_path):
    """输入带标点（天青色，等烟雨）→ 命中无标点行（天青色等烟雨）。"""
    song = _write_song(tmp_path, "青花瓷",
                       "[00:01.00]天青色等烟雨 而我在等你\n")
    idx = LyricIndex()
    idx.build([song])
    assert len(idx.search("天青色，等烟雨")) == 1
    assert len(idx.search("  天青色等烟雨  ")) == 1


def test_search_dedup_and_rank(tmp_path):
    a = _write_song(tmp_path, "A歌", "[00:01.00]天青色等烟雨 而我在等你\n")
    b = _write_song(tmp_path, "B歌", "[00:01.00]天青色等烟雨 而我在等你\n[00:02.00]别的词\n")
    idx = LyricIndex()
    idx.build([a, b])
    r = idx.search("天青色等烟雨")
    assert len(r) == 2  # 两首都命中（去重：同歌只一条）
    # 同分 → 排序稳定；再验证"部分相似"低于"完全包含"
    r2 = idx.search("天青色")
    assert r2[0]["title"] in ("A歌", "B歌")  # 包含命中 1.0 在前


def test_search_threshold_and_miss(tmp_path):
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    assert idx.search("完全不在的歌词内容") == []
    # 包含命中恒 1.0（不受阈值过滤）；纯相似弱匹配才会被高阈值滤掉
    assert len(idx.search("小黄")) == 1          # "小黄" ⊂ "小黄花" → 1.0
    r = idx.search("小黄草", threshold=0.95)     # 覆盖率 ~0.67 → 高阈值过滤
    assert r == []
    assert len(idx.search("小黄草")) == 1        # 默认阈值下模糊命中


def test_search_partial_ratio_long_line(tmp_path):
    """短片段 vs 长歌词行：ratio 被稀释（~0.37），覆盖率（成段命中）→ 1.0。"""
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    assert len(idx.search("吹着前奏")) == 1
    assert len(idx.search("放晴的那天")) == 1    # 片段在行中（跨词位置）


def test_search_empty_query(tmp_path):
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    assert idx.search("") == []
    assert idx.search("  ，。  ") == []


def test_search_before_build():
    idx = LyricIndex()
    assert idx.search("任何") == []  # 未构建 → 空结果不崩


# =================================================================== 失效/懒构建
def test_invalidate_rebuild(tmp_path):
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    assert idx.is_built()
    idx.invalidate()
    assert not idx.is_built()
    assert idx.search("小黄花") == []
    assert idx.build([song]) > 0  # 可重建


def test_ensure_built_with_db():
    db = MagicMock()
    db.get_all_songs.return_value = []
    idx = LyricIndex(db)
    assert idx.ensure_built() == 0
    assert idx.is_built()
    db.get_all_songs.assert_called_once()


def test_parse_lrc_multiple_timestamps():
    """同一行多时间戳 → 展开多条（复用现有解析器行为，歌词索引应收录同文本一次）。"""
    lines = parse_lrc("[00:01.00][00:03.00]重复词\n")
    assert len(lines) == 2


# =================================================================== UI：面板
def _make_panel(monkeypatch, tmp_path):
    from app.ui.panels.music_panel import MusicPanel

    p = MusicPanel(music=None)
    # 不启真线程：索引未建时只记录触发
    monkeypatch.setattr(p, "_start_lyric_build", lambda: None)
    return p


def test_panel_mode_combo_exists(tmp_path, monkeypatch):
    p = _make_panel(monkeypatch, tmp_path)
    assert p.cb_local_mode is not None
    assert p.cb_local_mode.itemText(0) == "按歌名"
    assert p.cb_local_mode.itemText(1) == "按歌词"
    assert p.cb_local_mode.currentText() == "按歌名"


def test_panel_title_search_regression(tmp_path, monkeypatch):
    """按歌名（默认）→ 走原 search_local 路径。"""
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    calls = []

    class FakeMusic:
        def __init__(self):
            self.db = MagicMock()

        def search_local(self, kw, limit=200):
            calls.append(kw)
            return [{"id": 1, "path": "x", "title": "晴天", "artist": "周杰伦"}]

    p._music = FakeMusic()
    p.edit_local.setText("晴天")
    p.cb_local_mode.setCurrentIndex(0)
    p._do_local_search()
    assert calls == ["晴天"]
    assert p.list_local.count() == 1


def test_panel_lyric_search_with_index(tmp_path, monkeypatch):
    """按歌词 + 索引已建 → idx.search 被调 + 结果进列表。"""
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    p._lyric_index = idx
    p._music = MagicMock()  # 仅占位，search 不碰 music
    p.cb_local_mode.setCurrentIndex(1)
    p.edit_local.setText("小黄花")
    p._do_local_search()
    assert p.list_local.count() == 1
    item = p.list_local.item(0)
    assert item.data(Qt.UserRole)["title"] == "晴天"  # Qt.UserRole
    assert "匹配 1 首" in p.lbl_scan_status.text()


def test_panel_lyric_search_no_result(tmp_path, monkeypatch):
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    p._lyric_index = idx
    p._music = MagicMock()
    p.cb_local_mode.setCurrentIndex(1)
    p.edit_local.setText("不存在的歌词")
    p._do_local_search()
    assert p.list_local.count() == 0
    assert "未找到包含该歌词的歌曲" in p.lbl_scan_status.text()


def test_panel_lyric_search_trigger_build(tmp_path, monkeypatch):
    """按歌词 + 索引未建 → 触发后台构建 + 记录 pending query。"""
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    started = []
    p._start_lyric_build = lambda: started.append(True)  # 覆盖 patch，防真线程
    p._music = MagicMock()
    p.cb_local_mode.setCurrentIndex(1)
    p.edit_local.setText("小黄花")
    p._do_local_search()
    assert started == [True]
    assert p._pending_lyric_query == "小黄花"
    assert "正在建立歌词索引" in p.lbl_scan_status.text()


def test_panel_lyric_build_done_continues(tmp_path, monkeypatch):
    """索引构建完成 → 自动继续执行 pending 搜索。"""
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])  # 直接建好，验证 done 回调路径
    p._lyric_index = idx
    p._music = MagicMock()
    p._pending_lyric_query = "小黄花"
    p._on_lyric_build_done(100)
    assert p._pending_lyric_query is None
    assert p.list_local.count() == 1


def test_panel_scan_done_invalidates_index(tmp_path, monkeypatch):
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    p._lyric_index = idx
    p._music = MagicMock()
    p._on_scan_done({"added": 1, "updated": 0, "skipped": 0, "errors": 0})
    assert not idx.is_built()  # 扫描完成 → 索引失效


def test_panel_local_play_reused(tmp_path, monkeypatch):
    """歌词结果项双击 → 复用 _on_local_play（UserRole 数据可播放）。"""
    from app.ui.panels.music_panel import MusicPanel

    p = _make_panel(monkeypatch, tmp_path)
    song = _write_song(tmp_path, "晴天", _LRC_QINGTIAN)
    idx = LyricIndex()
    idx.build([song])
    p._lyric_index = idx
    p._music = MagicMock()
    p.cb_local_mode.setCurrentIndex(1)
    p.edit_local.setText("小黄花")
    p._do_local_search()
    item = p.list_local.item(0)
    played = []
    p._music = MagicMock()
    p._music.play_song = lambda s: played.append(s)
    p._on_local_play(item)
    assert played and played[0]["title"] == "晴天"
