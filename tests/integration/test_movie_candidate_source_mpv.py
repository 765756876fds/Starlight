"""Generated video over loopback HTTP, temporary stores and owned null-output MPV."""
from collections import Counter
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import quote, urlsplit

import pytest

from tests.integration.test_movie_resume_mpv import media as media, qapp as qapp, sessions as sessions

pytestmark = pytest.mark.integration


def test_real_mpv_path_and_progress_follow_selected_cms_not_default(sessions, media):
    calls = Counter()
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            calls[urlsplit(self.path).path] += 1
            super().do_GET()
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(media[1].parent)))
    server.daemon_threads = False
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    s = sessions('cms-source-identity')
    a = f'http://127.0.0.1:{server.server_port}/'+quote(media[1].name)
    b = f'http://127.0.0.1:{server.server_port}/'+quote(media[2].name)
    try:
        def play(source, url):
            payload = dict(vod_id=7, vod_name='Generated test', _source=source,
                           _source_id=source, first_url=url, play_url='正片$'+url)
            assert s.ctl.play(payload)
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                path = s.mpv.get_property('path', timeout=.2)
                pos = s.mpv.get_property('time-pos', timeout=.2)
                if path == url and type(pos) in (int,float) and pos > .1:
                    return payload
                time.sleep(.02)
            pytest.fail('owned MPV did not decode the selected generated HTTP media')
        s.ctl._source_manager.get_active_name.return_value = '光速资源'
        play('非凡资源', a)
        assert calls[urlsplit(a).path] >= 1 and calls[urlsplit(b).path] == 0
        s.ctl._source_manager.get_active_name.return_value = '极速资源'
        assert s.mpv.get_property('path') == a
        play('极速资源', b)
        assert s.mpv.get_property('path') == b and s.ctl.current_item()['_source'] == '极速资源'
    finally:
        # Controller goes first; never kill MPV while a live controller still owns it.
        s.ctl.close()
        s.mpv.stop_process()
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive() and not s.mpv.is_running()
