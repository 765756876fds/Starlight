"""Task-oriented Qt checks with synthetic metadata and isolated playback doubles."""
from dataclasses import replace
from unittest.mock import Mock
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from app.music.online.models import OnlineTrack
from app.ui.panels.movie_panel import _MovieDetailDialog
from tests.ui.test_music_search_reliability import panel, qapp, wait_until  # noqa: F401
from tests.ui.test_movie_voice_window import scene  # noqa: F401


def tracks():
    a = OnlineTrack("甲来源", "one", "星桥", "纸鸢乐队", "夜航", 180)
    return a, replace(a, source_name="乙来源", track_id="two")


def test_search_is_content_first_with_explicit_single_play_and_versions(panel):
    p, music, _ = panel
    a, b = tracks()
    music.play_online = Mock(return_value=False)
    p._on_search_done({"fake":[a,b]}, display_query="星桥")
    assert p.cb_source.isHidden() and p.cb_search_filter.isHidden()
    assert p.list_results.count() == 1
    assert "来源" not in p.list_results.item(0).text()
    assert p._track_versions == [[a,b]]
    assert p.btn_result_play.isEnabled() and p.btn_versions.isEnabled()
    p.btn_result_play.click()
    wait_until(lambda: music.play_online.called)
    assert music.play_online.call_args.args == (a,)
    assert music.play_online.call_args.kwargs["_alternatives"] == (a,b)
    wait_until(lambda: p.btn_retry_online.isVisibleTo(p.results_page))
    p.btn_retry_online.click()
    wait_until(lambda: music.play_online.call_count == 2)
    p.search_options.click()
    assert not p.cb_source.isHidden()


def test_unknown_is_not_success_and_explicit_retry_stays_on_selected_track(panel):
    p, music, _ = panel
    a, _ = tracks()
    music.play_online = Mock(return_value=None)
    p._start_online_track(a, ())
    assert p.lbl_status.text() == "正在加载…"
    wait_until(lambda: "暂未确认" in p.lbl_status.text())
    assert "正在播放" not in p.lbl_status.text()
    assert not p.btn_retry_online.isHidden()


def test_technical_search_error_stays_in_logs(panel):
    p, _, _ = panel
    p._on_search_failed("HTTP 503 IPC JSON handler secret-fixture")
    assert p.lbl_status.text() == "搜索暂未完成，请重试。"
    assert not p.btn_retry_online.isHidden()


def test_new_request_owns_feedback_and_cancels_old_load_guard(panel):
    p, music, _ = panel
    entered, release = Event(), Event(); guards=[]
    def slow(track, **options):
        entered.set();assert release.wait(2);guards.append(options["_before_load"]());return False
    music.play_online = Mock(side_effect=slow)
    a,b=tracks()
    try:
        p._start_online_track(a,[a,b]);assert entered.wait(1)
        p._play_async(lambda: False, title="新歌",fail_msg="新歌暂时无法播放")
        wait_until(lambda:p.lbl_status.text()=="新歌暂时无法播放")
    finally:
        release.set()
    wait_until(lambda:bool(guards))
    assert guards == [False]
    assert p.lbl_status.text()=="新歌暂时无法播放"


def test_movie_information_has_no_play_action_but_keeps_details(scene):
    p = scene.panel
    info={"vod_name":"只有作品信息", "vod_content":"一段作品简介", "_source":"甲平台"}
    p._on_search_done([info])
    item=p.list_results.item(0)
    assert "甲平台" not in item.text() and "仅作品信息" in item.text()
    dlg=_MovieDetailDialog(info,p)
    assert not dlg.btn_play.isEnabled()
    assert "一段作品简介" in dlg.lbl_info.text()
    dlg.close()
    scene.mpv.loadfile.assert_not_called()


def test_movie_address_is_tryable_and_source_is_secondary(scene):
    p=scene.panel;it=dict(scene.item,_source="甲平台")
    p._on_search_done([it])
    assert "可选播放" in p.list_results.item(0).text()
    assert "甲平台" in p.list_results.item(0).toolTip()
    assert p.lbl_source.isHidden()
    dlg=_MovieDetailDialog(it,p)
    assert dlg.btn_play.isEnabled()
    dlg.close()


def test_movie_failure_is_actionable_without_protocol_details(scene):
    p,c=scene.panel,scene.controller
    p._on_playback_status(c._playback_gen,"FAILED","HTTP 403 CMS JSON IPC")
    assert p.lbl_status.text()=="暂时无法播放，请重试或换一个版本。"
    assert p.btn_alternate_startup.text()=="换一个版本"


def test_home_summary_uses_only_supplied_metadata_and_no_source_caption():
    from app.ui.widgets.movie_home import film_card
    data=film_card({"title":"星河旅人","kind":"movie","description":"<p>一次远行与重逢的故事。</p>",
                    "sources":[{"platform":"bilibili"}]})
    assert data["caption"]=="一次远行与重逢的故事。"
    assert "platform" not in data["caption"]


def test_repeated_activation_of_loading_song_does_not_create_second_load(panel):
    p,music,_=panel;a,b=tracks();entered,release=Event(),Event()
    def slow(*args,**options):
        entered.set();assert release.wait(2);return False
    music.play_online=Mock(side_effect=slow)
    try:
        p._start_online_track(a,[a,b]);assert entered.wait(1)
        p._start_online_track(a,[a,b])
        assert music.play_online.call_count==1
    finally:
        release.set()
    wait_until(lambda:p._manual_request is None)


@pytest.mark.parametrize("reason,count,failed,expected",[
    ("complete",1,0,"搜索完成"),("complete",1,1,"部分内容暂不可用"),
    ("complete",0,3,"搜索暂未完成"),("budget",1,1,"暂未加载"),("budget",0,1,"尚未完成"),
    ("complete",0,0,"没有匹配影片")])
def test_progressive_search_keeps_protocol_counts_in_logs(scene,reason,count,failed,expected):
    p=scene.panel
    p._progressive_is_current=Mock(return_value=True)
    p._on_search_done([scene.item] if count else [])
    result=dict(reason=reason,done=1,total=3,failed=failed,errors=1,timed_out=1,unattempted=1,limited=False)
    p._progressive_finished(1,result)
    assert expected in p.lbl_status.text()
    assert all(word not in p.lbl_status.text() for word in ("1/3","尚未尝试","超时1","来源"))
    if reason=="budget" or (not count and failed):
        assert p.btn_search.text()=="重试"
