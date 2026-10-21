# -*- coding: utf-8 -*-
"""纯函数播放状态机 PlaybackState 测试：正常流转 / 暂停恢复 / epoch 竞态 / 条件忽略。"""
import pytest

pytestmark = pytest.mark.unit

from app.music.playback_state import (
    PlaybackEffect,
    PlaybackEffectType,
    PlaybackEvent,
    PlaybackEventType,
    PlaybackState,
    PlaybackStatus,
    reduce,
)


def _ev(t, epoch=0, track=None, error=None):
    return PlaybackEvent(type=t, track=track, epoch=epoch, error_msg=error)


def _types(effects):
    return [e.type for e in effects]


TRACK_A = {"id": 1, "title": "A"}
TRACK_B = {"id": 2, "title": "B"}


def test_normal_playback_flow():
    """正常播放流转：PLAY_REQUESTED -> LOAD_SUCCESS -> IDLE_DETECTED。"""
    s = PlaybackState()
    # 发起播放：LOADING + epoch 递增 + LOAD_FILE effect
    s, fx = reduce(s, _ev(PlaybackEventType.PLAY_REQUESTED, track=TRACK_A))
    assert s.status == PlaybackStatus.LOADING
    assert s.current_track == TRACK_A
    assert s.epoch == 1
    assert _types(fx) == [PlaybackEffectType.LOAD_FILE]
    assert fx[0].payload["track"] == TRACK_A
    # 加载成功（携带 epoch 1）：PLAYING，无副作用
    s, fx = reduce(s, _ev(PlaybackEventType.LOAD_SUCCESS, epoch=1, track=TRACK_A))
    assert s.status == PlaybackStatus.PLAYING
    assert s.current_track == TRACK_A
    assert fx == []
    # 检测到播放结束：IDLE + SCHEDULE_NEXT_EPISODE
    s, fx = reduce(s, _ev(PlaybackEventType.IDLE_DETECTED, epoch=1))
    assert s.status == PlaybackStatus.IDLE
    assert s.current_track is None
    assert _types(fx) == [PlaybackEffectType.SCHEDULE_NEXT_EPISODE]
    assert fx[0].payload["track"] == TRACK_A, "切歌 effect 应携带上一曲"


def test_pause_resume_flow():
    """暂停恢复流转：PLAYING -> PAUSE_REQUESTED -> PAUSED -> RESUME_REQUESTED -> PLAYING。"""
    s = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=1)
    s, fx = reduce(s, _ev(PlaybackEventType.PAUSE_REQUESTED))
    assert s.status == PlaybackStatus.PAUSED
    assert _types(fx) == [PlaybackEffectType.PAUSE_MPV]
    s, fx = reduce(s, _ev(PlaybackEventType.RESUME_REQUESTED))
    assert s.status == PlaybackStatus.PLAYING
    assert _types(fx) == [PlaybackEffectType.RESUME_MPV]
    # 状态/代际号在暂停恢复中保持不变
    assert s.epoch == 1 and s.current_track == TRACK_A


def test_pause_ignored_when_not_playing():
    """非 PLAYING 态收到 PAUSE_REQUESTED / RESUME_REQUESTED 应被忽略。"""
    for st in (PlaybackStatus.IDLE, PlaybackStatus.LOADING, PlaybackStatus.PAUSED,
               PlaybackStatus.FAILED):
        s = PlaybackState(status=st, epoch=0)
        s2, fx = reduce(s, _ev(PlaybackEventType.PAUSE_REQUESTED))
        assert s2 is s and fx == [], f"{st} 下 PAUSE 应被忽略"
    s = PlaybackState(status=PlaybackStatus.IDLE, epoch=0)
    s2, fx = reduce(s, _ev(PlaybackEventType.RESUME_REQUESTED))
    assert s2 is s and fx == [], "IDLE 下 RESUME 应被忽略"


def test_epoch_race_old_load_success_dropped():
    """epoch 竞态：epoch 1 的 LOAD_SUCCESS 晚到（已被 epoch 2 覆盖）→ 丢弃。"""
    s = PlaybackState()
    s, fx1 = reduce(s, _ev(PlaybackEventType.PLAY_REQUESTED, track=TRACK_A))  # epoch -> 1
    assert s.epoch == 1
    s, fx2 = reduce(s, _ev(PlaybackEventType.PLAY_REQUESTED, track=TRACK_B))  # epoch -> 2
    assert s.epoch == 2
    assert s.current_track == TRACK_B
    # epoch 1 的加载成功晚到：必须被丢弃，状态与代际号都不变
    s2, fx = reduce(s, _ev(PlaybackEventType.LOAD_SUCCESS, epoch=1, track=TRACK_A))
    assert s2 is s, "过期 LOAD_SUCCESS 不得改变状态"
    assert fx == [], "过期 LOAD_SUCCESS 不得产生副作用"
    assert s2.status == PlaybackStatus.LOADING and s2.epoch == 2
    # epoch 2 的加载成功正常生效
    s3, fx = reduce(s, _ev(PlaybackEventType.LOAD_SUCCESS, epoch=2, track=TRACK_B))
    assert s3.status == PlaybackStatus.PLAYING
    assert s3.current_track == TRACK_B


def test_epoch_race_old_load_failed_dropped():
    """epoch 竞态：过期 LOAD_FAILED 丢弃；当前代际失败才转 FAILED + NOTIFY_UI。"""
    s = PlaybackState(status=PlaybackStatus.LOADING, current_track=TRACK_A, epoch=3)
    s2, fx = reduce(s, _ev(PlaybackEventType.LOAD_FAILED, epoch=2, error="old"))
    assert s2 is s and fx == [], "过期 LOAD_FAILED 不得改变状态"
    s3, fx = reduce(s, _ev(PlaybackEventType.LOAD_FAILED, epoch=3, error="boom"))
    assert s3.status == PlaybackStatus.FAILED
    assert s3.error_msg == "boom"
    assert _types(fx) == [PlaybackEffectType.NOTIFY_UI]
    assert fx[0].payload["error_msg"] == "boom"


def test_idle_detected_ignored_when_not_playing():
    """IDLE_DETECTED 在非 PLAYING 状态被忽略（不切歌）。"""
    for st in (PlaybackStatus.IDLE, PlaybackStatus.LOADING,
               PlaybackStatus.PAUSED, PlaybackStatus.FAILED):
        s = PlaybackState(status=st, current_track=TRACK_A, epoch=1)
        for et in (PlaybackEventType.IDLE_DETECTED, PlaybackEventType.EOF_DETECTED):
            s2, fx = reduce(s, _ev(et, epoch=1))
            assert s2 is s and fx == [], \
                f"{st} 下 {et} 应被忽略"


def test_idle_detected_ignored_when_epoch_mismatch():
    """epoch 不匹配的 IDLE_DETECTED 被忽略（防旧检测误切下一首）。"""
    s = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=5)
    for et in (PlaybackEventType.IDLE_DETECTED, PlaybackEventType.EOF_DETECTED):
        s2, fx = reduce(s, _ev(et, epoch=4))
        assert s2 is s and fx == [], f"{et} epoch 不匹配应被忽略"
    # epoch 匹配则生效
    s3, fx = reduce(s, _ev(PlaybackEventType.EOF_DETECTED, epoch=5))
    assert s3.status == PlaybackStatus.IDLE
    assert _types(fx) == [PlaybackEffectType.SCHEDULE_NEXT_EPISODE]


def test_stop_requested():
    """STOP_REQUESTED → IDLE + STOP_MPV（无论当前什么状态）。"""
    s = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=2)
    s2, fx = reduce(s, _ev(PlaybackEventType.STOP_REQUESTED))
    assert s2.status == PlaybackStatus.IDLE
    assert s2.current_track is None
    assert _types(fx) == [PlaybackEffectType.STOP_MPV]
    # 从 FAILED 也可停止
    s3 = PlaybackState(status=PlaybackStatus.FAILED, error_msg="x")
    s4, _ = reduce(s3, _ev(PlaybackEventType.STOP_REQUESTED))
    assert s4.status == PlaybackStatus.IDLE


def test_unknown_event_keeps_state():
    """未知事件：保持原状态、无副作用（防御性）。"""
    s = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=1)
    s2, fx = reduce(s, PlaybackEvent(type="unknown.type", epoch=1))
    assert s2 is s and fx == []


def test_load_success_keeps_epoch_and_track():
    """LOAD_SUCCESS 生效时保持代际号；未带 track 时沿用 LOADING 态 track。"""
    s = PlaybackState(status=PlaybackStatus.LOADING, current_track=TRACK_A, epoch=1)
    s2, fx = reduce(s, _ev(PlaybackEventType.LOAD_SUCCESS, epoch=1))
    assert s2.status == PlaybackStatus.PLAYING
    assert s2.epoch == 1
    assert s2.current_track == TRACK_A
    assert fx == []


def test_reducer_pure_no_side_effect_on_error():
    """纯函数保证：reducer 调用前后外部状态无任何变化（无 IO 痕迹可测）。"""
    import copy

    s = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=1)
    before = copy.deepcopy(s)
    s2, fx = reduce(s, _ev(PlaybackEventType.PAUSE_REQUESTED))
    assert s == before, "reducer 不得修改入参状态（返回新实例）"
    assert s2 is not s
    assert isinstance(s2, PlaybackState)
    assert isinstance(fx[0], PlaybackEffect)
