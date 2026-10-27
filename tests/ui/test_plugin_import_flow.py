"""
插件导入 UI 流程定向测试（无头 offscreen）：
- 导入 .js 插件 → 复制到插件目录 → 自动刷新桥接 → 真实结果报告；
- 刷新失败时报告失败（不假装成功）；
- 音乐面板在线音源下拉在插件/源变化后同步（保留选中，失效回退「全部」）。
"""
import os
import sys
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(fn, timeout=6.0, interval=0.02):
    """驱动 Qt 事件循环直到 fn() 为真（跨线程 Signal 经队列分发到主线程）。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if fn():
            return True
        time.sleep(interval)
    return False


def _make_plugin_file(tmp_path, name="zz_import_test.js"):
    """最小合法 MusicFree 插件文件。"""
    p = tmp_path / name
    p.write_text(
        'function search(k){return [{name:"T_"+k,artist:"a",id:"1",duration:60}]}\n'
        'function getMediaSource(id,q){return {url:"https://example.com/x.mp3",quality:q}}\n'
        'module.exports={platform:"zzimport",version:"0.0.1",search,getMediaSource};\n',
        encoding="utf-8",
    )
    return p


def _make_dialog(reload_result, on_sources_changed=None):
    from app.ui.dialogs.source_manage_dialog import SourceManageDialog
    mgr = MagicMock()
    mgr.get_all_sources.return_value = []
    dlg = SourceManageDialog(
        parent=None,
        on_sources_changed=on_sources_changed,
        manager=mgr,
        reload_plugins=lambda: reload_result,
    )
    return dlg


# ---------- 任务 B：导入成功后自动刷新 ----------

def test_import_js_plugin_copies_file_and_triggers_reload(qapp, tmp_path, monkeypatch):
    """导入有效插件：文件复制进 musicfree 目录，自动刷新桥接，报告成功。"""
    from app.ui.dialogs.source_manage_dialog import SourceManageDialog

    src = _make_plugin_file(tmp_path, "zz_import_test.js")
    copied = []
    msgs = []
    notified = []

    def fake_copy2(a, b):
        copied.append((a, b))
        with open(b, "w", encoding="utf-8") as f:
            f.write(src.read_text(encoding="utf-8"))

    def fake_info(parent, title, msg):
        msgs.append((title, msg))

    monkeypatch.setattr(SourceManageDialog, "_refresh_list", lambda self: None)
    with patch("shutil.copy2", side_effect=fake_copy2), \
         patch("app.ui.dialogs.source_manage_dialog.glass_info",
               side_effect=fake_info), \
         patch("app.ui.dialogs.source_manage_dialog.QFileDialog.getOpenFileName",
               return_value=(str(src), "")):
        dlg = _make_dialog((True, 9), on_sources_changed=lambda: notified.append(1))
        try:
            dlg._import_from_file()
            assert copied, "插件文件应被复制到插件目录"
            target = copied[0][1]
            assert os.path.basename(target) == "zz_import_test.js"
            assert os.path.dirname(target).endswith("musicfree")
            # 等后台刷新线程 → Signal → 主线程槽
            ok = _wait_until(lambda: len(msgs) > 0)
            assert ok, "刷新完成信号应到达主线程"
            title, msg = msgs[0]
            assert "导入成功" in title
            assert "9" in msg or "9" in title, "报告应含真实插件数"
            assert notified, "刷新成功后应同步外部（搜索界面）"
        finally:
            dlg.close()
            if copied and os.path.exists(copied[0][1]):
                os.remove(copied[0][1])


def test_import_refresh_failure_reports_failure(qapp, tmp_path, monkeypatch):
    """刷新失败：必须报告失败，不能假装导入后已可用。"""
    from app.ui.dialogs.source_manage_dialog import SourceManageDialog

    src = _make_plugin_file(tmp_path, "zz_import_fail.js")
    copied = []
    criticals = []
    infos = []

    def fake_copy2(a, b):
        copied.append(b)
        with open(b, "w", encoding="utf-8") as f:
            f.write(src.read_text(encoding="utf-8"))

    monkeypatch.setattr(SourceManageDialog, "_refresh_list", lambda self: None)
    with patch("shutil.copy2",
               side_effect=fake_copy2), \
         patch("app.ui.dialogs.source_manage_dialog.glass_critical",
               side_effect=lambda *a: criticals.append(a)), \
         patch("app.ui.dialogs.source_manage_dialog.glass_info",
               side_effect=lambda *a: infos.append(a)), \
         patch("app.ui.dialogs.source_manage_dialog.QFileDialog.getOpenFileName",
               return_value=(str(src), "")):
        dlg = _make_dialog((False, 8))
        try:
            dlg._import_from_file()
            ok = _wait_until(lambda: bool(criticals) or bool(infos))
            assert ok, "刷新结果信号应到达主线程"
            assert not infos, "刷新失败不得报告成功"
            assert criticals, "刷新失败应弹失败提示"
            assert "刷新失败" in criticals[0][1], "失败提示应明确指出刷新失败"
        finally:
            dlg.close()
            if copied and os.path.exists(copied[0]):
                os.remove(copied[0])


def test_import_detect_obfuscated_plugin_no_shell(qapp, tmp_path, monkeypatch):
    """混淆插件（标记在 500 字节后）导入：复制到 musicfree，绝不落到 JSON 空壳。"""
    from app.ui.dialogs.source_manage_dialog import SourceManageDialog, detect_plugin_type

    head = "var _0xod8='jsjiami.com.v7';'use strict';"
    pad = "_" * 600
    tail = "Object.defineProperty(exports,'__esModule',{value:true});const http=require('axios');module.exports={platform:'kk',version:'1',search:async function(){}};"
    src = tmp_path / "zz_obfuscated.js"
    src.write_text(head + pad + tail, encoding="utf-8")
    assert detect_plugin_type(src.read_text(encoding="utf-8")) == "musicfree"

    copied = []
    popped = []   # 记录弹窗调用，用于等待后台刷新信号送达后再退出 patch 上下文
    with patch("shutil.copy2",
               side_effect=lambda a, b: (copied.append(b) or open(b, "w", encoding="utf-8").write(open(a, encoding="utf-8").read()))), \
         patch("app.ui.dialogs.source_manage_dialog.glass_info",
               lambda *a: popped.append(("info", a))), \
         patch("app.ui.dialogs.source_manage_dialog.glass_critical",
               lambda *a: popped.append(("critical", a))), \
         patch("app.ui.dialogs.source_manage_dialog.QFileDialog.getOpenFileName",
               return_value=(str(src), "")), \
         patch.object(SourceManageDialog, "_refresh_list", lambda self: None):
        dlg = _make_dialog((True, 9))
        try:
            mgr = dlg.manager
            dlg._import_from_file()
            # 必须等后台刷新线程 → Signal 送达主线程（否则信号在下一个用例的
            # 事件循环里投递，此时 glass_* 已还原为真实模态弹窗 → 卡死）
            assert _wait_until(lambda: len(popped) > 0), "刷新结果应送达主线程"
            assert copied, "混淆插件也应复制进插件目录"
            assert os.path.basename(copied[0]).endswith(".js")
            assert mgr.import_from_json.call_count == 0, "不得退回 JSON 空壳导入"
        finally:
            dlg.close()
            if copied and os.path.exists(copied[0]):
                os.remove(copied[0])


# ---------- 任务 A：搜索界面同步 ----------

class _FakeMusic:
    def __init__(self, sources):
        self._sources = list(sources)

    def get_online_sources(self):
        return list(self._sources)

    def get_play_mode(self):
        return "sequential"

    def is_highlight_mode(self):
        return False

    def get_volume(self):
        return 70

    def set_volume(self, v):
        pass

    def search_local(self, kw=""):
        return []

    def get_current_song(self):
        return None


def test_music_panel_refresh_online_sources(qapp):
    """刷新后搜索下拉获得最新源列表；选中项保留，失效回退「全部」。"""
    from app.ui.panels.music_panel import MusicPanel

    fake = _FakeMusic(["js_bridge", "old_src"])
    panel = MusicPanel(music=fake)
    try:
        names = [panel.cb_source.itemData(i) for i in range(panel.cb_source.count())]
        assert names == [None, "js_bridge", "old_src"]

        # 选中 old_src → 外部源列表变化（old_src 失效，新增 new_src）
        idx = panel.cb_source.findData("old_src")
        panel.cb_source.setCurrentIndex(idx)
        fake._sources = ["js_bridge", "new_src"]
        panel.refresh_online_sources()

        names2 = [panel.cb_source.itemData(i) for i in range(panel.cb_source.count())]
        assert names2 == [None, "js_bridge", "new_src"], "下拉应更新为最新列表"
        assert panel.cb_source.currentData() is None, "失效选中项应回退「全部」"

        # 选中有效项 → 刷新后保留
        i2 = panel.cb_source.findData("js_bridge")
        panel.cb_source.setCurrentIndex(i2)
        panel.refresh_online_sources()
        assert panel.cb_source.currentData() == "js_bridge", "有效选中项应保留"
    finally:
        panel._timer.stop()


def test_music_panel_refresh_no_music_no_crash(qapp):
    from app.ui.panels.music_panel import MusicPanel
    panel = MusicPanel(music=None)
    try:
        panel.refresh_online_sources()   # 不应抛异常
    finally:
        panel._timer.stop()
