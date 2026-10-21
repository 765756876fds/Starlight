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
    """非 PLAYING/LOADING 态收到 PAUSE 应忽略；非 PAUSED 态收到 RESUME 应忽略。"""
    for st in (PlaybackStatus.IDLE, PlaybackStatus.PAUSED, PlaybackStatus.FAILED):
        s = PlaybackState(status=st, epoch=0)
        s2, fx = reduce(s, _ev(PlaybackEventType.PAUSE_REQUESTED))
        assert s2 is s and fx == [], f"{st} 下 PAUSE 应被忽略"
    # RESUME 仅在 PAUSED 生效（PAUSED 是合法恢复状态，不在此列）
    for st in (PlaybackStatus.IDLE, PlaybackStatus.FAILED):
        s = PlaybackState(status=st, epoch=0)
        s2, fx = reduce(s, _ev(PlaybackEventType.RESUME_REQUESTED))
        assert s2 is s and fx == [], f"{st} 下 RESUME 应被忽略"


def test_pause_in_loading_sets_pause_pending():
    """LOADING 态收到 PAUSE_REQUESTED：挂起 pause_pending（不忽略、不立即 PAUSE_MPV）。"""
    s = PlaybackState(status=PlaybackStatus.LOADING, epoch=1)
    ev = PlaybackEvent(type=PlaybackEventType.PAUSE_REQUESTED, epoch=1)
    new_state, effects = reduce(s, ev)
    assert new_state.status == PlaybackStatus.LOADING
    assert new_state.pause_pending is True
    assert effects == [], "LOADING 期间不立即产生 PAUSE_MPV"


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
    """epoch 竞态：过期 LOAD_FAILED 丢弃；当前代际失败（未熔断）转 FAILED + ADVANCE_QUEUE。"""
    s = PlaybackState(status=PlaybackStatus.LOADING, current_track=TRACK_A, epoch=3)
    s2, fx = reduce(s, _ev(PlaybackEventType.LOAD_FAILED, epoch=2, error="old"))
    assert s2 is s and fx == [], "过期 LOAD_FAILED 不得改变状态"
    s3, fx = reduce(s, _ev(PlaybackEventType.LOAD_FAILED, epoch=3, error="boom"))
    assert s3.status == PlaybackStatus.FAILED
    assert s3.error_msg == "boom"
    assert s3.fail_streak == 1
    assert _types(fx) == [PlaybackEffectType.ADVANCE_QUEUE]
    assert fx[0].payload["reason"] == "load_failed"


def test_load_success_ignored_when_not_loading():
    """状态防污染：非 LOADING（如 PAUSED）收到同 epoch 的迟到 LOAD_SUCCESS → 忽略。"""
    s = PlaybackState(status=PlaybackStatus.PAUSED, current_track=TRACK_A, epoch=2)
    for st in (PlaybackStatus.PAUSED, PlaybackStatus.PLAYING,
               PlaybackStatus.IDLE, PlaybackStatus.FAILED):
        s2 = PlaybackState(status=st, current_track=TRACK_A, epoch=2)
        s3, fx = reduce(s2, _ev(PlaybackEventType.LOAD_SUCCESS, epoch=2, track=TRACK_A))
        assert s3 is s2 and fx == [], f"{st} 收到迟到 LOAD_SUCCESS 必须忽略"
    # LOAD_FAILED 同样只在 LOADING 生效
    s4 = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=2)
    s5, fx = reduce(s4, _ev(PlaybackEventType.LOAD_FAILED, epoch=2, error="late"))
    assert s5 is s4 and fx == [], "PLAYING 收到迟到 LOAD_FAILED 必须忽略"


def test_circuit_breaker_after_3_failures():
    """熔断机制：连续 3 次 auto 切歌加载失败 → IDLE + fail_streak==3，不产生 ADVANCE_QUEUE。"""
    s = PlaybackState()
    for i in range(2):
        # auto 切歌：fail_streak 保留
        s, fx = reduce(s, PlaybackEvent(
            PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="auto"))
        assert s.status == PlaybackStatus.LOADING
        s, fx = reduce(s, _ev(PlaybackEventType.LOAD_FAILED,
                              epoch=s.epoch, error="boom"))
        assert s.status == PlaybackStatus.FAILED
        assert s.fail_streak == i + 1, f"第 {i+1} 次失败后 streak 应为 {i+1}"
        assert _types(fx) == [PlaybackEffectType.ADVANCE_QUEUE]
    # 第 3 次失败 → 熔断
    s, fx = reduce(s, PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="auto"))
    s, fx = reduce(s, _ev(PlaybackEventType.LOAD_FAILED,
                          epoch=s.epoch, error="boom"))
    assert s.status == PlaybackStatus.IDLE, "熔断后应回 IDLE（停止自动重试）"
    assert s.fail_streak == 3
    assert _types(fx) == [PlaybackEffectType.NOTIFY_UI, PlaybackEffectType.STOP_MPV], \
        "熔断时通知 UI 且必须追加 STOP_MPV（状态已 IDLE 但 mpv 可能还在响）"
    assert "连续 3 次" in fx[0].payload["error_msg"]


def test_pause_pending_during_loading():
    """LOADING 态收到 PAUSE_REQUESTED：挂起暂停标记；LOAD_SUCCESS 后转 PAUSED + PAUSE_MPV。"""
    s, _ = reduce(PlaybackState(), PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="user"))
    s2, fx = reduce(s, PlaybackEvent(PlaybackEventType.PAUSE_REQUESTED))
    assert s2.status == PlaybackStatus.LOADING, "加载中暂停不立即改状态"
    assert s2.pause_pending is True
    assert fx == [], "加载中暂停不产生即时 effect"
    s3, fx = reduce(s2, _ev(PlaybackEventType.LOAD_SUCCESS, epoch=s2.epoch, track=TRACK_A))
    assert s3.status == PlaybackStatus.PAUSED, "起播即暂停（防'起播即响'）"
    assert s3.pause_pending is False, "挂起标记已清空"
    assert s3.current_track == TRACK_A
    assert _types(fx) == [PlaybackEffectType.PAUSE_MPV]


def test_pause_pending_cleared_on_new_play():
    """PLAY_REQUESTED 清空 pause_pending（新播放意图不继承挂起暂停）。"""
    s, _ = reduce(PlaybackState(), PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="user"))
    s2, _ = reduce(s, PlaybackEvent(PlaybackEventType.PAUSE_REQUESTED))
    assert s2.pause_pending is True
    s3, _ = reduce(s2, PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_B, origin="user"))
    assert s3.pause_pending is False
    assert s3.status == PlaybackStatus.LOADING
    assert s3.epoch == s2.epoch + 1


def test_pause_pending_not_left_after_load_failed():
    """挂起暂停不泄漏：LOAD_FAILED（未熔断）转 FAILED 时清空 pause_pending。"""
    s, _ = reduce(PlaybackState(), PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="user"))
    s2, _ = reduce(s, PlaybackEvent(PlaybackEventType.PAUSE_REQUESTED))
    s3, fx = reduce(s2, _ev(PlaybackEventType.LOAD_FAILED, epoch=s2.epoch, error="boom"))
    assert s3.status == PlaybackStatus.FAILED
    assert _types(fx) == [PlaybackEffectType.ADVANCE_QUEUE]
    assert s3.pause_pending is False, "失败后不再挂起暂停"


def test_user_play_resets_circuit_breaker():
    """用户切歌重置熔断：连续 2 次失败后，origin='user' 的 PLAY_REQUESTED 清零 fail_streak。"""
    s = PlaybackState()
    for _ in range(2):
        s, _ = reduce(s, PlaybackEvent(
            PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="auto"))
        s, _ = reduce(s, _ev(PlaybackEventType.LOAD_FAILED,
                             epoch=s.epoch, error="boom"))
    assert s.fail_streak == 2
    # 用户主动切歌：清零
    s, fx = reduce(s, PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_B, origin="user"))
    assert s.status == PlaybackStatus.LOADING
    assert s.fail_streak == 0, "用户切歌必须清零失败计数"
    assert s.current_track == TRACK_B
    # auto 切歌则保留
    s2, _ = reduce(s, _ev(PlaybackEventType.LOAD_FAILED, epoch=s.epoch))
    s3, _ = reduce(s2, PlaybackEvent(
        PlaybackEventType.PLAY_REQUESTED, track=TRACK_A, origin="auto"))
    assert s3.fail_streak == 1, "auto 切歌保留现有 fail_streak"


def test_classify_grace_period():
    """classify 宽限期：LOADING + elapsed=0.5s + idle=True → None（等待起播）。"""
    from app.music.playback_state import classify_status
    s = PlaybackState(status=PlaybackStatus.LOADING, current_track=TRACK_A, epoch=1)
    ev = classify_status(s, {"idle": True, "playback_time": 0.0, "duration": 0.0},
                         elapsed_sec=0.5, kind="local")
    assert ev is None, "宽限期内不判定"


def test_classify_success():
    """classify 成功判定：LOADING + elapsed=1.0s + duration=180 → LOAD_SUCCESS（带当前 epoch）。"""
    from app.music.playback_state import classify_status
    s = PlaybackState(status=PlaybackStatus.LOADING, current_track=TRACK_A, epoch=3)
    ev = classify_status(s, {"idle": False, "playback_time": 0.0, "duration": 180.0},
                         elapsed_sec=1.0, kind="local")
    assert ev is not None
    assert ev.type == PlaybackEventType.LOAD_SUCCESS
    assert ev.epoch == 3, "classify 结果必须携带当前 epoch"
    assert ev.track == TRACK_A


def test_classify_failed_and_timeout():
    """classify 失败判定：宽限期后 idle → LOAD_FAILED；online 超 10s 未起播 → LOAD_FAILED。"""
    from app.music.playback_state import classify_status
    s = PlaybackState(status=PlaybackStatus.LOADING, current_track=TRACK_A, epoch=1)
    ev = classify_status(s, {"idle": True, "playback_time": 0.0, "duration": 0.0},
                         elapsed_sec=1.2, kind="local")
    assert ev is not None and ev.type == PlaybackEventType.LOAD_FAILED
    assert ev.epoch == 1
    # 宽限期（online 3.0s）内 idle 不判失败
    ev2 = classify_status(s, {"idle": True, "playback_time": 0.0, "duration": 0.0},
                          elapsed_sec=2.0, kind="online")
    assert ev2 is None
    # online 超 10s 且无播放时间 → 超时失败
    ev3 = classify_status(s, {"idle": False, "playback_time": 0.0, "duration": 0.0},
                          elapsed_sec=11.0, kind="online")
    assert ev3 is not None and ev3.type == PlaybackEventType.LOAD_FAILED
    assert ev3.error_msg == "在线加载超时"
    # 超时判定仅限 online：local 无此语义
    ev4 = classify_status(s, {"idle": False, "playback_time": 0.0, "duration": 0.0},
                          elapsed_sec=11.0, kind="local")
    assert ev4 is None


def test_classify_playing_idle_detected():
    """classify 播放中 idle → IDLE_DETECTED；其他状态一律 None。"""
    from app.music.playback_state import classify_status
    s = PlaybackState(status=PlaybackStatus.PLAYING, current_track=TRACK_A, epoch=1)
    ev = classify_status(s, {"idle": True, "playback_time": 10.0, "duration": 180.0},
                         elapsed_sec=30.0, kind="local")
    assert ev is not None and ev.type == PlaybackEventType.IDLE_DETECTED
    assert ev.epoch == 1
    ev2 = classify_status(s, {"idle": False, "playback_time": 10.0, "duration": 180.0},
                          elapsed_sec=30.0, kind="local")
    assert ev2 is None
    # 非 LOADING/PLAYING 状态：一律 None
    s_idle = PlaybackState(status=PlaybackStatus.IDLE, epoch=1)
    assert classify_status(s_idle, {"idle": True, "playback_time": 0.0, "duration": 0.0},
                           elapsed_sec=1.0, kind="local") is None


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
