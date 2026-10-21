"""本地封面三级机制（图 9 回归）：FLAC PICTURE / MP3 APIC → QImage 非空。

无真实音乐文件时用 mutagen 写入内嵌封面后跑 _LocalCoverTask.run 验证提取链路。
"""
import base64
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytestmark = pytest.mark.ui


# 合法 PNG 运行时生成（QImage 编码，避免手写 base64 CRC 出错）
_PNG_CACHE = {}


def _png_bytes(qapp):
    if not _PNG_CACHE:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(QColor(210, 60, 60))
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        _PNG_CACHE["b"] = bytes(buf.data())
    return _PNG_CACHE["b"]


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _write_flac(path, qapp):
    from mutagen.flac import FLAC, Picture
    pic = Picture()
    pic.type = 3
    pic.mime = "image/png"
    pic.desc = "cover"
    pic.data = _png_bytes(qapp)
    f = FLAC(FLAC.__new__(FLAC))  # 空文件对象
    f.add_picture(pic)
    f.save(path)


def _write_mp3(path, qapp):
    from mutagen.id3 import ID3, APIC
    tags = ID3()
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="cover",
                  data=_png_bytes(qapp)))
    tags.save(path, v2_version=3)  # ID3v2.3：APIC 帧键为 "APIC"（无冒号）


def _run_cover(path):
    from PySide6.QtCore import QObject, Signal
    from app.ui.widgets.music_card_delegate import _LocalCoverTask

    class Bridge(QObject):
        local_done = Signal(str, object)

    b = Bridge()
    got = {}
    b.local_done.connect(lambda k, img: got.update({k: img}))
    _LocalCoverTask("local:x", path, b).run()
    return got.get("local:x")


def test_flac_inner_cover_extracted(monkeypatch, qapp):
    # FLAC 封面是 PICTURE 块（pictures 属性）：mock mutagen 对象验证提取分支
    from app.ui.widgets import music_card_delegate as mcd

    class _FakePic:
        data = _png_bytes(qapp)

    class _FakeFLAC:
        tags = None
        pictures = [_FakePic()]

    monkeypatch.setattr(mcd, "_MutagenFile", lambda p: _FakeFLAC())
    img = _run_cover("x.flac")
    assert img is not None and not img.isNull(), "FLAC PICTURE 封面未提取"


def test_mp3_v23_inner_cover_extracted(monkeypatch, qapp):
    # ID3v2.3 APIC（键无冒号）：此前只查 "APIC:" 会漏掉 v2.3
    from app.ui.widgets import music_card_delegate as mcd

    class _FakeFrame:
        data = _png_bytes(qapp)

    class _FakeMP3:
        tags = {"APIC": _FakeFrame()}

    monkeypatch.setattr(mcd, "_MutagenFile", lambda p: _FakeMP3())
    img = _run_cover("x.mp3")
    assert img is not None and not img.isNull(), "MP3 ID3v2.3 APIC 封面未提取"


def test_mp3_v24_inner_cover_extracted(monkeypatch, qapp):
    # ID3v2.4 APIC（键带冒号）既有分支不回归
    from app.ui.widgets import music_card_delegate as mcd

    class _FakeFrame:
        data = _png_bytes(qapp)

    class _FakeMP3:
        tags = {"APIC:": _FakeFrame()}

    monkeypatch.setattr(mcd, "_MutagenFile", lambda p: _FakeMP3())
    img = _run_cover("x.mp3")
    assert img is not None and not img.isNull(), "MP3 ID3v2.4 APIC 封面未提取"


def test_mp3_apic_roundtrip(qapp, tmp_path):
    from mutagen.id3 import ID3
    p = str(tmp_path / "c.mp3")
    _write_mp3(p, qapp)
    back = ID3(p)
    frames = back.getall("APIC")
    assert frames and bytes(frames[0].data) == _png_bytes(qapp)


def test_no_inner_falls_back_to_folder_cover(qapp, tmp_path):
    p = str(tmp_path / "song.mp3")
    with open(p, "wb") as f:
        f.write(b"\x00")
    cover = tmp_path / "folder.jpg"
    cover.write_bytes(_png_bytes(qapp))
    img = _run_cover(p)
    assert img is not None and not img.isNull(), "同目录 folder.jpg 未兜底"


def test_cover_task_lists_folder_names():
    from app.ui.widgets.music_card_delegate import _LocalCoverTask
    assert "cover.jpg" in _LocalCoverTask._NAMES
    assert "folder.jpg" in _LocalCoverTask._NAMES
    assert "front.jpg" in _LocalCoverTask._NAMES
