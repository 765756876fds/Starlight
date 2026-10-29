"""哼唱识曲测试：增量建库 / 识别成功失败 / 录音 wav / 弹窗状态机 / 语音入口。"""
import pytest

pytestmark = pytest.mark.ui

import os
import sys
import types
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from app.core.command_bus import Command, CommandType
from app.music.humming import HummingError, HummingService, _pyaudio_record


# =================================================================== 建库
def _fake_audio_dir(tmp_path, names=("a.mp3", "b.flac", "c.wav")):
    d = tmp_path / "songs"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x" * 100)
    return str(d)


class _FakeSummary:
    indexed = 2
    failed = 0


def test_index_pending_incremental(tmp_path):
    """增量建库：第一批建 3 首（2 成功 1 失败），第二批全部已入库跳过。"""
    service = HummingService([_fake_audio_dir(tmp_path)], str(tmp_path / "fp.db"))
    hits = {"n": 0}

    def find(p):
        hits["n"] += 1
        return None if hits["n"] <= 3 else object()

    rt = SimpleNamespace(
        storage=SimpleNamespace(find_by_filepath=find, flush=lambda: None),
        indexer=SimpleNamespace(index_paths=lambda paths, **kw: _FakeSummary()),
    )
    service._runtime = rt
    r1 = service.index_pending()
    assert r1["indexed"] == 2 and r1["skipped"] == 1 and r1["failed"] == 0
    r2 = service.index_pending()
    assert r2["indexed"] == 0 and r2["skipped"] == 3 and r2["failed"] == 0


def test_index_pending_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    service = HummingService([str(d)], str(tmp_path / "fp.db"))
    assert service.index_pending() == {"indexed": 0, "skipped": 0, "failed": 0}


def test_index_pending_skips_unsupported(tmp_path):
    d = tmp_path / "mixed"
    d.mkdir()
    (d / "a.mp3").write_bytes(b"x")
    (d / "b.txt").write_bytes(b"x")
    service = HummingService([str(d)], str(tmp_path / "fp.db"))
    assert len(service._scan_audio()) == 1


# =================================================================== 识别
def test_identify_hit(tmp_path):
    wav = tmp_path / "q.wav"
    wav.write_bytes(b"RIFF")
    service = HummingService([], str(tmp_path / "fp.db"))
    tr = SimpleNamespace(title="晴天", artist="周杰伦",
                         filepath=r"D:\落雪\音乐文件\晴天.flac", filename="晴天.flac")
    from app.third_party.fingerprint.core.matcher import Occurrence
    m = SimpleNamespace(track=tr, confidence=0.9, aligned_hashes=120,
                        peak_ratio=88.0,
                        best=Occurrence(100, 120, 0.9, 88.0, 0, 20))
    query = SimpleNamespace(frames_to_seconds=lambda frames: frames * 0.425)
    rt = SimpleNamespace(search_file=lambda p: SimpleNamespace(matches=[m], query=query))
    service._runtime = rt
    r = service.identify_file(str(wav))
    assert r["title"] == "晴天" and r["artist"] == "周杰伦"
    assert r["filepath"] == r"D:\落雪\音乐文件\晴天.flac"
    assert abs(r["confidence"] - 0.9) < 1e-9
    assert r["offset_sec"] == 42.5
    assert r["quality"]  # strong/likely/weak 分级存在
    assert "aligned" in r and "peak_ratio" in r


def test_identify_miss(tmp_path):
    wav = tmp_path / "q.wav"
    wav.write_bytes(b"RIFF")
    service = HummingService([], str(tmp_path / "fp.db"))
    rt = SimpleNamespace(search_file=lambda p: SimpleNamespace(matches=[]))
    service._runtime = rt
    assert service.identify_file(str(wav)) is None


def test_identify_missing_file(tmp_path):
    service = HummingService([], str(tmp_path / "fp.db"))
    try:
        service.identify_file(str(tmp_path / "none.wav"))
        assert False, "应抛 HummingError"
    except HummingError:
        pass


def test_has_library(tmp_path):
    service = HummingService([], str(tmp_path / "fp.db"))
    service._runtime = SimpleNamespace(storage=SimpleNamespace(count_tracks=lambda: 3))
    assert service.has_library() is True


# =================================================================== 录音
def test_record_clip_writes_wav(tmp_path, monkeypatch):
    class FakeStream:
        def __init__(self):
            self.reads = 0

        def read(self, n, exception_on_overflow=False):
            self.reads += 1
            return b"\x00\x00" * 512

        def stop_stream(self):
            pass

        def close(self):
            pass

    class FakePyAudio:
        def __init__(self):
            self.terminated = False

        def open(self, **kw):
            self.kw = kw
            return FakeStream()

        def terminate(self):
            self.terminated = True

    fake_mod = types.ModuleType("pyaudio")
    fake_mod.PyAudio = FakePyAudio
    fake_mod.paInt16 = 8
    monkeypatch.setitem(sys.modules, "pyaudio", fake_mod)

    out = str(tmp_path / "rec.wav")
    _pyaudio_record(out, seconds=1, sample_rate=16000)
    with wave.open(out, "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() > 0


def test_record_no_pyaudio(tmp_path, monkeypatch):
    """pyaudio 缺失 → HummingError（不崩）。sys.modules 置 None 使 import 抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "pyaudio", None)
    try:
        _pyaudio_record(str(tmp_path / "x.wav"), seconds=1)
        assert False, "应抛 HummingError"
    except HummingError:
        pass


# =================================================================== 弹窗状态机
def _make_dialog(tmp_path, monkeypatch, has_library=True):
    from app.ui.dialogs.identify_dialog import IdentifyDialog

    service = HummingService([], str(tmp_path / "fp.db"))
    service._runtime = SimpleNamespace(
        storage=SimpleNamespace(count_tracks=lambda: 1 if has_library else 0))
    dlg = IdentifyDialog(service, on_play=lambda r: None)
    monkeypatch.setattr(dlg, "_start_identify_worker", lambda wav, source="microphone", cancel_event=None: None)  # 不启真线程
    return dlg


def test_dialog_recording_then_identifying(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    dlg.start()
    dlg.begin()
    assert dlg._busy is True
    assert dlg._countdown == dlg._service.record_seconds
    assert "采集中" in dlg.lbl_status.text()
    dlg._on_tick()
    assert dlg._countdown == dlg._service.record_seconds - 1
    for _ in range(dlg._service.record_seconds):
        dlg._on_tick()
    assert "识别中" in dlg.lbl_status.text()


def test_dialog_success_card(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    dlg._begin_record()
    dlg._on_done({"title": "晴天", "artist": "周杰伦",
                  "confidence": 0.9, "quality": "strong"})
    assert not dlg._busy
    assert "晴天" in dlg.lbl_title.text()
    assert not dlg.btn_play.isHidden()
    assert dlg._result is not None


def test_dialog_miss(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    dlg._begin_record()
    dlg._on_done(None)
    assert "未找到匹配结果" in dlg.lbl_status.text()
    assert dlg.btn_play.isHidden()


def test_dialog_failed(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    dlg._begin_record()
    dlg._on_failed("麦克风被占用")
    assert "识别失败" in dlg.lbl_status.text()
    assert "麦克风" in dlg.lbl_status.text()
    assert not dlg._busy


def test_dialog_cancelled_can_start_again(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    dlg._begin_record()
    dlg._on_cancelled()
    assert not dlg._busy
    assert "可以重新开始" in dlg.lbl_status.text()
    assert dlg.btn_start.isEnabled()


def test_dialog_empty_library_prompts_without_auto_build(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch, has_library=False)
    dlg.start()
    assert dlg._busy is False
    dlg.begin()
    assert "请先点击" in dlg.lbl_status.text()


def test_dialog_index_button_explicitly_starts_build(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch, has_library=False)
    calls = []
    monkeypatch.setattr(dlg, "_show_build", lambda: calls.append(True))
    dlg.btn_index.click()
    assert calls == [True]


def test_dialog_index_done_does_not_start_recording(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(dlg, "_begin_record", lambda: started.append(True))
    dlg._on_build_done({"indexed": 4, "skipped": 2, "failed": 1})
    assert started == []
    assert "新增 4 首" in dlg.lbl_status.text()
    assert "失败 1 首" in dlg.lbl_status.text()


def test_dialog_source_selection(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    assert dlg.source_combo.currentData() == "system"
    dlg.source_combo.setCurrentIndex(1)
    assert dlg._source == "microphone"
    dlg.source_combo.setCurrentIndex(0)
    assert dlg._source == "system"


def test_dialog_build_failed_no_longer_auto_runs(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch, has_library=False)
    dlg.start()
    assert not dlg._busy


def test_dialog_busy_blocks_restart(tmp_path, monkeypatch):
    dlg = _make_dialog(tmp_path, monkeypatch)
    dlg._begin_record()
    assert dlg._busy is True
    dlg.begin()  # busy 中 → 忽略
    assert dlg._countdown == dlg._service.record_seconds  # 未重置（未重新 begin）


# =================================================================== 语音入口
def test_intent_identify_rules():
    from app.voice.intent import RuleBasedIntent

    for text in ("听听这是什么歌", "听歌识曲", "帮我找找这首歌",
                 "这是什么歌", "识曲", "帮我听听这是什么歌", "帮我识曲"):
        it = RuleBasedIntent.match(text)
        assert it is not None, f"未命中: {text}"
        assert it.intent_type == CommandType.MUSIC_IDENTIFY, f"{text} → {it.intent_type}"


def test_intent_identify_no_conflict_with_search():
    """"播放晴天"等搜索命令不得被识曲规则吞掉。"""
    from app.voice.intent import RuleBasedIntent

    it = RuleBasedIntent.match("播放晴天")
    assert it is None or it.intent_type != CommandType.MUSIC_IDENTIFY
    it2 = RuleBasedIntent.match("清空队列")
    assert it2 is not None and it2.intent_type == CommandType.MUSIC_CLEAR_QUEUE


def test_runtime_dispatches_identify():
    from app.voice.intent import Intent
    from app.voice.runtime import VoiceRuntime

    rt = VoiceRuntime(config={})
    calls = []
    rt.command_bus = SimpleNamespace(execute=lambda c: calls.append(c.command_type))
    rt._execute_intent(Intent(CommandType.MUSIC_IDENTIFY, params={}), "听听这是什么歌")
    assert calls == [CommandType.MUSIC_IDENTIFY]


# =================================================================== 面板
def test_panel_identify_button_and_handler():
    from app.core.command_bus import get_command_bus
    from app.ui.panels.music_panel import MusicPanel

    p = MusicPanel(music=None)
    try:
        assert p.btn_identify is not None
        assert "识曲" in p.btn_identify.text()
        assert get_command_bus().has_handler(CommandType.MUSIC_IDENTIFY)
    finally:
        get_command_bus().unregister(CommandType.MUSIC_IDENTIFY)


def test_panel_identify_cmd_opens_dialog():
    from app.ui.panels.music_panel import MusicPanel

    class FakeDialog:
        def __init__(self):
            self.shown = 0
            self.started = 0

        def show(self):
            self.shown += 1

        def raise_(self):
            pass

        def start(self):
            self.started += 1

    p = MusicPanel(music=None)
    try:
        d = FakeDialog()
        p._identify_dialog = d
        p._on_identify_cmd(Command(CommandType.MUSIC_IDENTIFY))
        assert d.shown == 1 and d.started == 1
    finally:
        from app.core.command_bus import get_command_bus
        get_command_bus().unregister(CommandType.MUSIC_IDENTIFY)


def test_panel_identify_button_opens_dialog(monkeypatch):
    from app.ui.panels import music_panel
    from app.ui.panels.music_panel import MusicPanel

    class FakeService:
        record_seconds = 8

        def has_library(self):
            return False

    monkeypatch.setattr(music_panel, "HummingService", lambda **kwargs: FakeService())
    p = MusicPanel(music=None)
    try:
        p.btn_identify.click()
        assert p._identify_dialog is not None
        assert p._identify_dialog.isVisible()
        assert p._identify_dialog.lbl_status.text() == "准备就绪"
    finally:
        p._identify_dialog.hide()
        from app.core.command_bus import get_command_bus
        get_command_bus().unregister(CommandType.MUSIC_IDENTIFY)


def test_panel_identify_button_reports_service_init_failure(monkeypatch):
    from app.ui.panels import music_panel
    from app.ui.panels.music_panel import MusicPanel

    def fail(**kwargs):
        raise RuntimeError("audiofp 未就绪")

    monkeypatch.setattr(music_panel, "HummingService", fail)
    p = MusicPanel(music=None)
    try:
        p.btn_identify.click()
        assert "识曲暂不可用" in p.lbl_identify_status.text()
        assert "audiofp 未就绪" in p.lbl_identify_status.text()
    finally:
        from app.core.command_bus import get_command_bus
        get_command_bus().unregister(CommandType.MUSIC_IDENTIFY)


def test_panel_identify_play():
    from app.ui.panels.music_panel import MusicPanel

    plays = []

    class FakeMusic:
        def __init__(self):
            self.db = SimpleNamespace(
                get_song_by_path=lambda fp: {"path": fp, "title": "晴天"})
            self.play_song = lambda s: plays.append(s)

    p = MusicPanel(music=None)
    try:
        p._music = FakeMusic()
        p._on_identify_play({"filepath": r"D:\落雪\音乐文件\晴天.flac"})
        assert plays and plays[0]["title"] == "晴天"
        # 无 filepath / 无库命中 → 静默返回
        p._on_identify_play({})
        assert len(plays) == 1
    finally:
        from app.core.command_bus import get_command_bus
        get_command_bus().unregister(CommandType.MUSIC_IDENTIFY)
