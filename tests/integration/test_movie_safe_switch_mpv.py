"""Two generated HLS origins, an isolated MPV and the Qt search child."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from app.movie.playback_diagnostics import read_sample
from app.movie.safe_switch import context_for
from app.movie.search_process import SearchProcessPool
from app.movie.switch_probe import PROBE_MS, verify
from tests.integration.test_movie_resume_mpv import qapp as qapp, sessions as sessions, media as media
from tests.unit.test_movie_safe_switch import item

pytestmark = pytest.mark.integration


@pytest.fixture
def routes(media, tmp_path):
    ffmpeg = media[0].parents[1] / 'ffmpeg/ffmpeg.exe'
    directory = tmp_path / 'hls'
    directory.mkdir()
    manifest = directory / 'movie.m3u8'
    subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=black:s=160x90:r=10:d=28', '-f', 'lavfi',
                    '-i', 'anullsrc=r=48000:cl=stereo', '-shortest', '-c:a', 'mp2', '-b:a', '64k', '-c:v', 'mpeg2video', '-threads', '2', '-bf', '0',
                    '-g', '20', '-sc_threshold', '0', '-hls_time', '2',
                    '-hls_playlist_type', 'vod', '-hls_segment_filename', str(directory / '%03d.ts'), str(manifest)], timeout=20, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
    servers, threads = [], []
    flags = [False, False]
    released = threading.Event()

    alternate = tmp_path / "alternate"
    shutil.copytree(directory, alternate)

    def create(i):
        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                if flags[i] == "stall":
                    if self.path == "/010.ts" and "StarlightMediaProbe" not in self.headers.get("User-Agent", ""):
                        released.wait(24)
                elif flags[i]:
                    self.send_error(503)
                    return
                try:
                    super().do_GET()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(directory if i == 0 else alternate)))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        servers.append(server)
        threads.append(t)
        return f'http://127.0.0.1:{server.server_port}/movie.m3u8'

    yield (create(0), create(1)), flags, directory
    released.set()
    for server, t in zip(servers, threads):
        server.shutdown()
        server.server_close()
        t.join(2)
        assert not t.is_alive()


def wait(app, predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    pytest.fail('owned HLS/MPV deadline')


def test_real_hls_probe_shared_child_and_changed_manifest(qapp, routes):
    urls, flags, directory = routes
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp)
    pool = SearchProcessPool(qapp)
    results = []
    pool.returned.connect(lambda _, result: results.append(result))
    try:
        assert pool.start('proof', dict(mode='verify_switch', original=urls[0], candidate=urls[1], loopback=True), PROBE_MS)
        wait(qapp, lambda: results)
        assert results[0]['status'] == 'ok'
        assert results[0]['duration'] == 28 and 0 < results[0]['bytes'] < 8*1024*1024
        print("verified_bytes", results[0]["bytes"], "worker_elapsed_ms", results[0]["total_elapsed_ms"])
        changed = directory.parent / "alternate" / "000.ts"
        data = changed.read_bytes()
        changed.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        difference = verify(dict(original=urls[0], candidate=urls[1], loopback=True))
        assert difference['status'] == 'failed' and difference['bytes'] > 0
        flags[1] = True
        assert verify(dict(original=urls[0], candidate=urls[1], loopback=True))['status'] == 'failed'
        assert pool.peak == 1
        assert len(list(Path(directory).glob('*.ts'))) == 14
    finally:
        pool.shutdown()
        pool.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize('candidate_disappears', [False, True])
def test_real_mpvs_exact_start_and_failure_restore(qapp, routes, sessions, monkeypatch, candidate_disappears):
    # This case injects only the transport-stall admission flag. Position,
    # duration, media identity, load/seek progress and rollback are real IPC.
    from app.movie import safe_switch
    urls, flags, _ = routes
    s = sessions('switch-hls')
    s.mpv.extra_video_args.remove('--no-audio')
    original, other = [dict(item(name), play_url='正片$'+url, first_url=url)
                       for name, url in zip(('first', 'second'), urls)]
    try:
        assert s.ctl.play(original)
        wait(qapp, lambda: (read_sample(s.mpv.ipc_pipe, expected_path=urls[0]).get('time-pos') or 0) > .1)
        assert s.mpv._send_command('seek', [6, 'absolute+exact'])['error'] == 'success'
        pos = wait(qapp, lambda: (lambda x: x if x and 6 <= x < 7 else None)(
            read_sample(s.mpv.ipc_pipe, expected_path=urls[0]).get('time-pos')))
        context = context_for(s.ctl, [other])
        assert context is not None
        assert safe_switch.context_current(s.ctl, context)
        assert context.original == urls[0] and context.target == urls[1]

        def admission_sample(pipe, timeout, *, expected_path, **options):
            result = read_sample(pipe, timeout, expected_path=expected_path, **options)
            if expected_path == urls[0] and not getattr(admission_sample, 'read', False):
                admission_sample.read = True
                result['paused-for-cache'] = True
            return result

        monkeypatch.setattr(safe_switch, 'read_sample', admission_sample)
        flags[1] = candidate_disappears
        result = s.ctl.switch_verified_source(context, 28, lambda: True)
        expected = urls[int(not candidate_disappears)]
        assert result == ('restored' if candidate_disappears else 'started')
        evidence = read_sample(s.mpv.ipc_pipe, expected_path=expected)
        assert evidence.get('time-pos', -1) >= pos-.75
        assert s.ctl.current_item()['_source_id'] == ('first' if candidate_disappears else 'second')
    finally:
        s.ctl.close()
        s.mpv.stop_process()
        assert not s.mpv.is_running()



def test_real_stall_verified_then_explicitly_switch_to_local_mirror(qapp, routes, sessions):
    from types import SimpleNamespace
    from app.ui.theme.manager import get_theme_manager
    from app.ui.widgets.movie_switch_trial import MovieSwitchTrial

    get_theme_manager(qapp)
    urls, flags, _ = routes
    flags[0] = 'stall'
    s = sessions('switch-stall')
    s.mpv.extra_video_args.remove('--no-audio')
    s.mpv.extra_video_args += [
        '--cache=yes', '--demuxer-max-bytes=256KiB', '--demuxer-readahead-secs=2',
        '--cache-secs=2', '--cache-pause-initial=yes', '--cache-pause-wait=0.5',
    ]
    original, other = [dict(item(name), play_url='正片$'+url, first_url=url)
                       for name, url in zip(('first', 'second'), urls)]
    pool = SearchProcessPool(qapp)
    trial = MovieSwitchTrial(s.ctl, qapp)
    trial.enabled = True
    outcomes = []
    trial.completed.connect(outcomes.append)
    try:
        assert s.ctl.set_source_quality_trial(True)
        assert s.ctl.play(original)
        manager = SimpleNamespace(get_search_revision=lambda: 1,
                                  get_enabled_sources=lambda: [{'url': u} for u in urls])
        trial.prepare(SimpleNamespace(pool=pool, request=None, manager=manager), [other])
        wait(qapp, lambda: (read_sample(s.mpv.ipc_pipe, expected_path=urls[0]).get('time-pos') or 0) > .1)
        assert not trial.poll(True) and not pool.active
        last = [-1]
        def offer():
            report = (s.ctl.source_quality_snapshot() or {}).get("current")
            if report and last[0] != report.observed_at:
                last[0] = report.observed_at
                sample = read_sample(s.mpv.ipc_pipe, expected_path=urls[0])
                print("quality evidence", report.state, report.reason, report.cache_seconds,
                      report.wait_lower_s, report.progress_s, report.sustained_transport_wait,
                      sample.get("demuxer-cache-state"), sample.get("demuxer-cache-idle"))
            return trial.poll(True)
        wait(qapp, offer, seconds=30)
        pos = read_sample(s.mpv.ipc_pipe, expected_path=urls[0])['time-pos']
        assert s.ctl.current_item() is original and pos >= 8
        trial.confirm()
        wait(qapp, lambda: outcomes, seconds=8)
        assert outcomes[0][1] == 'started'
        evidence = read_sample(s.mpv.ipc_pipe, expected_path=urls[1])
        assert evidence['time-pos'] >= pos-.75 and evidence['video-params']['w'] == 160
        assert s.ctl.current_item()['_source_id'] == 'second'
        assert not trial.poll(True) and pool.peak == 1
    finally:
        trial.reset()
        pool.shutdown()
        s.ctl.close()
        s.mpv.stop_process()
        trial.deleteLater()
        pool.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not s.mpv.is_running()
