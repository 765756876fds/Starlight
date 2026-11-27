"""Qt task snapshot and batch action coverage using a controlled manager."""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import pytest

from app.music.online.models import OnlineTrack
from app.ui.dialogs.download_tasks_dialog import DownloadTasksPane, download_label
from tests.ui.test_music_search_reliability import panel, qapp

pytestmark = pytest.mark.ui


def task(key, state, **fields):
    return {
        "key": key, "state": state, "generation": 1,
        "song": {"title": key, "artist": "歌手"}, **fields,
    }


def test_download_metrics_only_use_real_known_values():
    unknown = download_label(task("旧歌", "downloading", bytes_written=512, speed_bps=255))
    assert unknown == "正在下载 · 512 B · 255 B/s"
    known = download_label(task("新歌", "downloading", bytes_written=2048, total_bytes=8192,
                                speed_bps=512, eta_seconds=12))
    assert known == "正在下载 · 2.0 KiB / 8.0 KiB (25%) · 512 B/s · 剩约 12 秒"
    paused = download_label(task("新歌", "paused", bytes_written=2048, total_bytes=8192,
                                 speed_bps=512, eta_seconds=12))
    assert paused == "已暂停 · 2.0 KiB / 8.0 KiB (25%)"
    finished = download_label(task("新歌", "completed",
                                   details={"format": "FLAC", "quality": "无损", "size_bytes": 8192}))
    assert finished == "下载完成 · FLAC · 无损 · 8.0 KiB"
    damaged = download_label(task("旧记录", "downloading", bytes_written=True, total_bytes="-1",
                                  speed_bps=float("inf"), eta_seconds="未知",
                                  details={"size_bytes": "坏数据", "format": 1, "quality": 0}))
    assert damaged == "正在下载"


def test_batch_actions_target_selected_eligible_keys_and_keep_rows_stable(qapp):
    pane = DownloadTasksPane(None)
    actions = []
    pane.operation_requested.connect(lambda name, keys: actions.append((name, set(keys))))
    pane.show()
    try:
        pane.update_task("A", task("A", "downloading"))
        pane.update_task("B", task("B", "queued"))
        pane.update_task("C", task("C", "failed"))
        row_a, row_b, row_c = (pane.rows[key] for key in "ABC")
        pane.items.setCurrentItem(row_a)
        row_b.setSelected(True)
        row_c.setSelected(True)
        pane.pause.click()
        assert actions[-1] == ("pause", {"A", "B"})
        pane.retry.click()
        assert actions[-1] == ("retry", {"C"})
        pane.update_task("A", task("A", "pausing"))
        pane.update_task("B", task("B", "paused"))
        assert [pane.items.item(i) for i in range(3)] == [row_a, row_b, row_c]
        assert {item.data(Qt.UserRole) for item in pane.items.selectedItems()} == {"A", "B", "C"}
        pane.resume.click()
        assert actions[-1] == ("resume", {"B"})
        pane.cancel_action.trigger()
        assert actions[-1] == ("cancel", {"B"})
        pane.remove_action.trigger()
        assert actions[-1] == ("remove", {"B", "C"})
        pane.forget("B")
        assert [pane.items.item(i) for i in range(2)] == [row_a, row_c]
    finally:
        pane.close()


class TaskSnapshots:
    def __init__(self, *records):
        self.records = {record["key"]: record for record in records}
        self.actions = []

    @staticmethod
    def task_key(song, options=None):
        return f"{song['source_name']}:{song['track_id']}"

    def tasks(self):
        return [dict(record) for record in self.records.values()]

    def submit(self, song, *, options=None):
        key = self.task_key(song, options)
        if key in self.records:
            return False
        self.records[key] = task(key, "queued", song=song, options=options)
        return True

    def pause(self, key):
        self.actions.append(("pause", key))
        self.records[key]["state"] = "pausing"
        return True

    def remove(self, key):
        self.actions.append(("remove", key))
        del self.records[key]
        return True


def test_batch_search_uses_exact_sources_and_preserves_existing_task_list(panel):
    p, music, errors = panel
    store = TaskSnapshots(task("previous", "paused", bytes_written=37))
    music.downloader = store
    music.download_song = store.submit
    songs = [
        OnlineTrack("owner", "first", "相同曲名", "同一歌手", "现场版", 184,
                    extra={"_raw": {"isrc": "USABC0000001"}}),
        OnlineTrack("owner", "second", "相同曲名", "同一歌手", "专辑版", 200,
                    extra={"_raw": {"isrc": "USABC0000002"}}),
    ]
    p.resize(850, 670)
    p.show()
    p.tabs.setCurrentIndex(1)
    p._on_search_done({"owner": songs})
    p.online_stack.setCurrentWidget(p.results_page)
    listing = p.list_results
    assert listing.count() == 2
    p._toggle_view_mode(False)
    QTest.qWait(10)
    listing.setCurrentRow(0)
    QTest.mouseClick(listing.viewport(), Qt.LeftButton, Qt.ControlModifier,
                     pos=listing.visualItemRect(listing.item(1)).center())
    assert len(listing.selectedItems()) == 2 and p.btn_download_selected.isVisible()
    p.btn_download_selected.click()
    assert set(store.records) == {"previous", "owner:first", "owner:second"}
    assert store.records["owner:first"]["song"]["track_id"] == "first"
    assert store.records["owner:second"]["song"]["track_id"] == "second"
    pane = p._download_pane
    prior = pane.rows["previous"]
    pane.items.setCurrentItem(pane.rows["owner:first"])
    pane.rows["owner:second"].setSelected(True)
    pane.pause.click()
    assert set(store.actions) == {("pause", "owner:first"), ("pause", "owner:second")}
    assert pane.rows["previous"] is prior
    assert "37 B" in prior.text()
    assert all(pane.tasks[key]["state"] == "pausing" for key in ("owner:first", "owner:second"))
    p._close_media_pane(animate=False)
    p._show_download_tasks()
    assert pane.rows["previous"] is prior
    pane.items.setCurrentItem(prior)
    pane.remove_action.trigger()
    assert "previous" not in pane.rows
    assert errors == []
