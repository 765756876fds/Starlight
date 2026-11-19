"""Matching CMS metadata does not authorize an unverified media handoff."""
from types import SimpleNamespace
from unittest.mock import Mock

from app.movie.safe_switch import context_current, context_for, handoff
from app.movie.source_quality import SourceQualitySession
from app.movie.switch_probe import playlist
import pytest


def item(source='first', kind='电影', episode='正片', identity='tt1234567'):
    url = f'https://{source}.example/movie.m3u8'
    return dict(vod_name='Generated', vod_year='2024', type_name=kind, imdb_id=identity,
                vod_id=source, _source_id=source, play_url=f'{episode}${url}', first_url=url)


def fake_controller():
    process = SimpleNamespace(poll=lambda: None)
    current = item()
    controller = Mock(_current_index=0, _playback_gen=4, _play_attempt=10, _media_voice_closed=False)
    controller.current_item.return_value = current
    controller.mpv._process = process
    return controller


@pytest.mark.parametrize('row', [item('alt', identity=''), item('alt', episode='第2集'), item('alt', kind='电视剧')])
def test_same_title_cannot_authorize_another_version_or_episode(row):
    assert context_for(fake_controller(), [row]) is None


@pytest.mark.parametrize('line', ['#EXT-X-DISCONTINUITY', '#EXT-X-KEY:METHOD=AES-128',
                                  '#EXT-X-BYTERANGE:12', '#EXT-X-STREAM-INF:BANDWIDTH=1000'])
def test_unsupported_hls_timeline_is_rejected(line):
    body = ('#EXTM3U\n#EXTINF:2,\na.ts\n' + line + '\n#EXTINF:2,\nb.ts\n#EXT-X-ENDLIST').encode()
    with pytest.raises(ValueError):
        playlist(body, 'https://source.example/vod.m3u8')


def test_old_generation_and_late_confirmation_do_not_mutate_player():
    controller = fake_controller()
    context = context_for(controller, [item('alt')])
    assert context_current(controller, context)
    controller._playback_gen += 1
    assert handoff(controller, context, 50, lambda: True) == 'stale'
    controller.mpv._send_command.assert_not_called()


@pytest.mark.parametrize('cache_idle,cache_duration,delta,eligible', [
    (False, .2, 0, True), (True, 6, 0, False), (False, 4, 0, False), (False, .2, .5, False),
])
def test_real_transport_threshold_requires_depleted_cache_and_stalled_progress(
    cache_idle, cache_duration, delta, eligible,
):
    quality = SourceQualitySession('cms', 'origin', 1, 0)
    stable = {'cache-speed': 25, 'video-params': {'w': 1280, 'h': 720}, 'speed': 1.,
              'pause': False, 'seeking': False, 'core-idle': False, 'eof-reached': False,
              'paused-for-cache': False, 'demuxer-cache-duration': 4.}
    for now in range(10):
        quality.observe(dict(stable, **{'time-pos': now}), now)
    for now in range(10, 16):
        report = quality.observe(dict(stable, **{'time-pos': 9+(now-10)*delta,
            'paused-for-cache': True, 'demuxer-cache-idle': cache_idle,
            'demuxer-cache-duration': cache_duration}), now)
    assert report.sustained_transport_wait is eligible
    assert report.resolution == (1280, 720) and report.input_bps_median == 25
    for key in ('pause', 'seeking', 'eof-reached'):
        result = quality.observe(dict(stable, **{'time-pos': 10, key: True}), 16)
        assert result.state == 'EXCLUDED' and not result.sustained_transport_wait

def test_unconfirmed_load_retains_original_without_second_load(monkeypatch):
    from threading import Lock
    from app.movie import safe_switch
    controller = fake_controller()
    controller.mpv._pipe_lock = Lock()
    context = context_for(controller, [item("alt")])
    monkeypatch.setattr(safe_switch, "read_sample", lambda *a, **k: {
        "time-pos": 8, "duration": 30, "pause": False, "seeking": False, "paused-for-cache": True,
    })
    controller.mpv._send_command.side_effect = [{"data": [{"type": "video"}]}, None]
    assert handoff(controller, context, 30, lambda: True) == "unknown"
    assert controller.current_item() is context.item and controller._playback_gen == context.generation
    assert controller._switch_unsettled
    assert [c.args[0] for c in controller.mpv._send_command.call_args_list] == ["get_property", "loadfile"]


def test_loopback_requires_explicit_isolated_probe_permission():
    from app.movie.switch_probe import verify
    result = verify({"original": "http://127.0.0.1:1/test.m3u8",
                     "candidate": "http://127.0.0.1:2/test.m3u8"})
    assert result == {"status": "failed", "items": [], "error": "UnverifiedMedia", "bytes": 0}


def test_late_manual_control_never_rolls_back_or_adopts_a_new_source(monkeypatch):
    from threading import Lock
    from app.movie import safe_switch
    controller = fake_controller()
    controller._switch_control_revision = 1
    controller.mpv._pipe_lock = Lock()
    context = context_for(controller, [item("alt")])
    monkeypatch.setattr(safe_switch, "read_sample", lambda *a, **k: {
        "time-pos": 8, "duration": 30, "pause": False, "seeking": False, "paused-for-cache": True,
    })
    controller.mpv._send_command.side_effect = [{"data": [{"type": "video"}]}, {"error": "success"}]

    def late_control(*_):
        controller._switch_control_revision += 1
        return False

    monkeypatch.setattr(safe_switch, "_progress", late_control)
    assert handoff(controller, context, 30, lambda: True) == "unknown"
    assert controller.current_item() is context.item and controller._switch_unsettled
    assert [c.args[0] for c in controller.mpv._send_command.call_args_list] == ["get_property", "loadfile"]
