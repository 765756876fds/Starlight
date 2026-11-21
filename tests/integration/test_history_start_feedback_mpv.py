"""History selection stays available until generated video actually advances."""
from PySide6.QtWidgets import QDialog, QLabel

from app.ui.panels.movie_panel import MoviePanel
from tests.integration.test_movie_resume_mpv import media, qapp, sessions  # noqa: F401
from tests.ui.test_music_search_reliability import wait_until


def test_history_dialog_waits_for_real_video(sessions, media, qapp):
    s = sessions()
    panel = MoviePanel(movie=s.ctl)
    panel._timer.stop()
    dialog = QDialog(panel)
    dialog.playback_status = QLabel(dialog)
    row = {"title": "generated", "media_type": "local", "replay_value": str(media[1])}
    try:
        dialog.show()
        assert panel._play_movie_history_row(row, dialog)
        assert dialog.isVisible() and "等待起播确认" in dialog.playback_status.text()

        def advance():
            update = s.ctl.poll_film_ui()
            panel._apply_film_poll(None, update, None)
            return dialog.result() == QDialog.Accepted

        # The fixture deliberately starts MPV paused. Release it only after checking the receipt.
        s.mpv.set_property("pause", False)
        wait_until(advance, timeout=6)
        assert s.ctl.playback_startup().state == "started"
        assert s.mpv.get_property("time-pos", timeout=.3) > 0
    finally:
        panel.close()
