"""Real multi-source search/panel/controller, local CMS, mock MPV and temporary data."""
from copy import deepcopy
import threading
import time

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QListWidget

from app.ui.panels.movie_panel import _MovieDetailDialog
from tests.ui.test_movie_progressive_ui import screen as screen, begin
from tests.ui.test_movie_progressive_search import cms as cms, wait
from tests.ui.test_movie_voice_window import scene as scene

pytestmark = pytest.mark.ui


@pytest.fixture
def candidates(screen, cms):
    manager = screen.controller._source_manager
    for row, name in zip(manager._sources, ('光速资源', '非凡资源', '极速资源')):
        row['name'] = name
    manager.set_active('光速资源')
    for i in range(3):
        cms.modes[f'/s{i}'] = dict(rows=[dict(vod_id=7, vod_name='Public same title', vod_pic='',
                                             vod_play_from=f'route-{i}',
                                             vod_play_url=f'正片$https://media-{i}.invalid/movie.m3u8')])
    screen.panel._refresh_source_label()
    return screen


def found(s):
    begin(s)
    wait(lambda: '搜索完成' in s.panel.lbl_status.text())
    return {s.panel.list_posters.item(i).data(Qt.UserRole)['_source']: s.panel.list_posters.item(i)
            for i in range(s.panel.list_posters.count())}


@pytest.mark.parametrize('default,chosen,index', [('光速资源','非凡资源',1), ('非凡资源','极速资源',2)])
def test_clicked_row_source_reaches_mpv_without_default_source_rewrite(candidates, default, chosen, index):
    s = candidates
    manager = s.controller._source_manager
    manager.set_active(default)
    rows = found(s)
    snapshot = deepcopy(rows[chosen].data(Qt.UserRole))
    original = s.path.read_bytes()
    s.panel._on_item_activated(rows[chosen])
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with(f'https://media-{index}.invalid/movie.m3u8')
    assert s.controller.current_item()['_source'] == chosen
    assert manager.get_active_name() == default and s.path.read_bytes() == original
    assert snapshot == rows[chosen].data(Qt.UserRole)


def test_label_distinguishes_default_and_current_item_source(candidates):
    s = candidates
    rows = found(s)
    assert '默认搜索源：光速资源' in s.panel.lbl_source.text()
    s.panel._on_item_activated(rows['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    assert '当前影片来源：非凡资源' in s.panel.lbl_source.text()
    assert '默认搜索源：光速资源' in s.panel.lbl_source.text()
    s.controller._source_manager.set_active('极速资源')
    s.panel._refresh_source_label()
    assert '当前影片来源：非凡资源' in s.panel.lbl_source.text()
    assert '默认搜索源：极速资源' in s.panel.lbl_source.text()
    s.mpv.loadfile.assert_called_once_with('https://media-1.invalid/movie.m3u8')


def test_same_id_detail_button_uses_its_own_payload(candidates):
    s = candidates
    rows = found(s)
    assert all(i.data(Qt.UserRole)['vod_id'] == 7 for i in rows.values())
    keys = {s.panel._search_poster_key(dict(row.data(Qt.UserRole), vod_pic='https://poster.invalid/same.jpg'))
            for row in rows.values()}
    assert len(keys) == 3
    detail = _MovieDetailDialog(deepcopy(rows['非凡资源'].data(Qt.UserRole)), s.panel)
    detail.btn_play.click()
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with('https://media-1.invalid/movie.m3u8')
    detail.close()


def test_multiple_lines_do_not_concatenate_urls(candidates, cms):
    s = candidates
    cms.modes['/s1']['rows'][0].update(vod_play_from='main$$$backup',
        vod_play_url='正片$https://media-1.invalid/main.m3u8$$$正片$https://media-1.invalid/backup.m3u8')
    row = found(s)['非凡资源']
    s.panel._on_item_activated(row)
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with('https://media-1.invalid/main.m3u8')
    assert '$$$' in s.controller.current_item()['vod_play_url']  # Original data survives.


@pytest.mark.parametrize('response,reason', [(None,'未确认'), ({'error':'loading failed'},'暂时无法播放')])
def test_nonempty_url_failure_is_visible_and_not_reported_as_missing_address(candidates, response, reason):
    s = candidates
    rows = found(s)
    s.mpv.loadfile.return_value = response
    s.panel._on_item_activated(rows['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    assert reason in s.panel.lbl_status.text()
    assert s.controller.playback_startup().state == ("load_unknown" if response is None else "load_rejected")
    assert '无可用地址' not in s.panel.lbl_status.text()
    assert s.controller.current_item() is None


def test_empty_play_address_is_explicit_and_does_not_load_another_cms(candidates, cms):
    s = candidates
    cms.modes['/s1']['rows'][0]['vod_play_url'] = ''
    row = found(s)['非凡资源']
    s.panel._on_item_activated(row)
    wait(lambda: '暂时没有可用片源' in s.panel.lbl_status.text() or '无法播放' in s.panel.lbl_status.text())
    assert '暂时没有可用片源' in s.panel.lbl_status.text()
    s.mpv.loadfile.assert_not_called()


def test_detail_explicit_backup_line_keeps_same_source(candidates, cms):
    s = candidates
    cms.modes['/s1']['rows'][0].update(vod_play_from='main$$$backup',
        vod_play_url='正片$https://media-1.invalid/main.m3u8$$$正片$https://media-1.invalid/backup.m3u8')
    row = found(s)['非凡资源']
    original = deepcopy(row.data(Qt.UserRole))
    detail = _MovieDetailDialog(original, s.panel)
    detail.combo_play_line.setCurrentIndex(1)
    detail.btn_play.click()
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with('https://media-1.invalid/backup.m3u8')
    assert s.controller.current_item()['_source'] == '非凡资源'
    assert row.data(Qt.UserRole) == original
    detail.close()


def test_series_line_selection_episode_and_sidebar_stay_with_candidate(candidates, cms, monkeypatch):
    from app.ui.widgets.glass_dialog import GlassDialog
    s = candidates
    cms.modes['/s1']['rows'][0].update(vod_play_from='main$$$backup',
        vod_play_url='一$https://media-1.invalid/a1#二$https://media-1.invalid/a2$$$一$https://media-1.invalid/b1#二$https://media-1.invalid/b2')
    row = found(s)['非凡资源']
    original_exec = GlassDialog.exec
    def choose(dialog):
        def select():
            combo = dialog.findChild(QComboBox, 'moviePlaybackLine')
            episodes = dialog.findChild(QListWidget, 'moviePlaybackEpisodes')
            combo.setCurrentIndex(1)
            episodes.itemDoubleClicked.emit(episodes.item(1))
        QTimer.singleShot(0, select)
        return original_exec(dialog)
    monkeypatch.setattr(GlassDialog, 'exec', choose)
    s.panel._on_item_activated(row)
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with('https://media-1.invalid/b2')
    assert s.controller.current_item()['_source'] == '非凡资源'
    assert s.panel._video_win.sidebar.episode_count() == 2
    s.mpv.reset_mock()
    s.panel._video_win.switch_episode(0)
    s.mpv.loadfile.assert_called_once_with('https://media-1.invalid/b1')


def test_accepted_is_not_start_confirmation_and_pending_status_uses_existing_poll(candidates, monkeypatch):
    s = candidates
    s.panel._on_item_activated(found(s)['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    assert '正在加载' in s.panel.lbl_status.text()
    monkeypatch.setattr(s.controller, 'tick', lambda: None)
    monkeypatch.setattr(s.controller, 'get_position', lambda: 0.)
    monkeypatch.setattr(s.controller, 'get_duration', lambda: 120.)
    window = s.panel._video_win
    window._load_started_at = time.monotonic()-16
    window._tick()
    assert '正在加载' in s.panel.lbl_status.text()
    monkeypatch.setattr(s.controller, 'get_position', lambda: 3.0)
    window._tick()
    assert '正在加载' in s.panel.lbl_status.text()
    s.mpv.loadfile.assert_called_once()


def test_late_status_cannot_overwrite_new_playback(candidates):
    s = candidates
    rows = found(s)
    s.panel._on_item_activated(rows['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    old_generation = s.controller._playback_gen
    s.panel._on_item_activated(rows['极速资源'])
    wait(lambda: s.mpv.loadfile.call_count == 2)
    current = s.panel.lbl_status.text()
    s.panel._on_playback_status(old_generation, 'UNCONFIRMED', 'old failure')
    assert s.panel.lbl_status.text() == current
    assert s.controller.current_item()['_source'] == '极速资源'


def test_load_ending_before_progress_is_reported_unconfirmed_without_retry(candidates, monkeypatch):
    s = candidates
    s.panel._on_item_activated(found(s)['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    monkeypatch.setattr(s.controller, 'tick', lambda: None)
    s.controller._current = None
    s.panel._video_win._tick()
    assert '暂未确认播放' in s.panel.lbl_status.text()
    s.mpv.loadfile.assert_called_once()


def test_unknown_new_load_does_not_allow_old_pending_notice_to_overwrite_it(candidates, monkeypatch):
    s = candidates
    rows = found(s)
    s.panel._on_item_activated(rows['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.return_value = None
    s.panel._on_item_activated(rows['极速资源'])
    wait(lambda: s.mpv.loadfile.call_count == 2)
    message = s.panel.lbl_status.text()
    assert '暂未确认播放' in message
    monkeypatch.setattr(s.controller, 'tick', lambda: None)
    monkeypatch.setattr(s.controller, 'get_position', lambda: 0.)
    monkeypatch.setattr(s.controller, 'get_duration', lambda: 120.)
    window = s.panel._video_win
    window._load_started_at = time.monotonic()-16
    window._tick()
    assert s.panel.lbl_status.text() == message
    assert s.mpv.loadfile.call_count == 2


def test_voice_presentation_after_failed_click_clears_old_error_and_source_caption(candidates):
    s = candidates
    rows = found(s)
    s.mpv.loadfile.return_value = {'error':'loading failed'}
    s.panel._on_item_activated(rows['非凡资源'])
    wait(lambda: s.mpv.loadfile.called)
    assert not s.panel._video_win._recover_label.isHidden()
    s.mpv.loadfile.return_value = {'error':'success'}
    assert s.controller.play(rows['极速资源'].data(Qt.UserRole))
    s.panel._do_embed_attach()
    assert s.panel._video_win._recover_label.isHidden()
    assert '当前影片来源：极速资源' in s.panel.lbl_source.text()
    assert s.mpv.loadfile.call_count == 2


@pytest.mark.parametrize('choose_late', [False, True])
def test_first_or_appended_candidate_keeps_its_own_url_and_selection(candidates, cms, choose_late):
    s = candidates
    gate = threading.Event()
    cms.modes['/s0']['wait'] = cms.modes['/s1']['wait'] = gate
    try:
        begin(s)
        wait(lambda: s.panel.list_posters.count() == 1)
        first = s.panel.list_posters.item(0)
        assert first.data(Qt.UserRole)['_source'] == '极速资源'
        s.panel.list_posters.setCurrentItem(first)
        chosen = first
        expected = 'https://media-2.invalid/movie.m3u8'
        if choose_late:
            gate.set()
            wait(lambda: s.panel.list_posters.count() == 3)
            assert s.panel.list_posters.currentItem() is first
            chosen = next(s.panel.list_posters.item(i) for i in range(3)
                          if s.panel.list_posters.item(i).data(Qt.UserRole)['_source'] == '非凡资源')
            expected = 'https://media-1.invalid/movie.m3u8'
        s.panel._on_item_activated(chosen)
        wait(lambda: s.mpv.loadfile.called)
        s.mpv.loadfile.assert_called_once_with(expected)
    finally:
        gate.set()


def test_voice_takeover_replaces_sidebar_candidate_before_next_episode(candidates, cms):
    s = candidates
    for i in (1,2):
        cms.modes[f'/s{i}']['rows'][0]['vod_play_url'] = (
            f'一$https://media-{i}.invalid/one#二$https://media-{i}.invalid/two')
    rows = found(s)
    s.panel._play_item(rows['非凡资源'].data(Qt.UserRole), index=0)
    wait(lambda: s.mpv.loadfile.called)
    assert s.controller.play(rows['极速资源'].data(Qt.UserRole), index=0)
    s.panel._do_embed_attach()
    s.mpv.reset_mock()
    s.panel._video_win.play_next()
    s.mpv.loadfile.assert_called_once_with('https://media-2.invalid/two')
    assert s.controller.current_item()['_source'] == '极速资源'


@pytest.mark.parametrize("identity", ["same_work", "title_only", "different_episode"])
def test_failed_candidate_recovery_only_after_user_action(candidates, cms, identity):
    screen = candidates
    panel = screen.panel
    for idx in range(3):
        row = cms.modes[f"/s{idx}"]["rows"][0]
        row.update(vod_year="2024", type_name="电影")
        if identity != "title_only":
            row["imdb_id"] = "tt1234567"
        if identity == "different_episode" and idx != 1:
            row["vod_play_url"] = f"第2集$https://media-{idx}.invalid/movie.m3u8"
    rows = found(screen)
    screen.mpv.loadfile.return_value = {"error": "loading failed"}
    panel._on_item_activated(rows["非凡资源"])
    wait(lambda: not panel.btn_retry_startup.isHidden())
    screen.mpv.loadfile.assert_called_once_with("https://media-1.invalid/movie.m3u8")
    assert not panel.btn_back_to_results.isHidden()
    assert panel.btn_alternate_startup.isHidden() == (identity != "same_work")
    if identity == "same_work":
        panel.btn_alternate_startup.click()
        wait(lambda: screen.mpv.loadfile.call_count == 2)
        assert screen.mpv.loadfile.call_args.args[0] in (
            "https://media-0.invalid/movie.m3u8", "https://media-2.invalid/movie.m3u8")
    else:
        panel.btn_retry_startup.click()
        wait(lambda: screen.mpv.loadfile.call_count == 2)
        assert screen.mpv.loadfile.call_args.args[0] == "https://media-1.invalid/movie.m3u8"
    panel.edit_kw.setText("Another title")
    panel._do_search()
    assert panel.btn_retry_startup.isHidden() and panel.btn_alternate_startup.isHidden()


@pytest.mark.parametrize("response", [None, {"error": "success"}])
def test_double_activation_does_not_reissue_pending_or_unknown_load(candidates, response):
    screen = candidates
    panel = screen.panel
    row = found(screen)["非凡资源"]
    screen.mpv.loadfile.return_value = response
    panel._on_item_activated(row)
    panel._on_item_activated(row)
    wait(lambda: screen.mpv.loadfile.called)
    panel._on_item_activated(row)
    screen.mpv.loadfile.assert_called_once_with("https://media-1.invalid/movie.m3u8")
    assert panel.btn_retry_startup.isHidden() and panel.btn_alternate_startup.isHidden()
