"""Queued access to existing UI actions; workers never touch a QWidget."""

from PySide6.QtCore import QObject, Signal, Slot, Qt

from ...core.command_bus import CommandType
from ...core.notifications import notify, operation_scope
from ...core.operation_result import OperationResult, OperationStatus as S


class MediaVoiceBridge(QObject):
    requested = Signal(object)

    def __init__(self, runtime, tabs, parent=None):
        super().__init__(parent or tabs)
        self.runtime, self.tabs = runtime, tabs
        self.tab_widget = getattr(tabs, "tabs", tabs)
        self.closed = False
        self.requested.connect(self._apply, Qt.QueuedConnection)
        runtime.command_bus.register(CommandType.MEDIA_UI, self.post)
        self.tab_widget.currentChanged.connect(self._page_changed)
        self._page_changed()
        self.destroyed.connect(lambda: setattr(self, "closed", True))

    def _page_changed(self, *_):
        current = self.tab_widget.currentWidget()
        domain = next((d for d in ("music", "movie") if self.tabs._panels.get(d) is current), None)
        self.runtime.media_context.note_page(domain)

    def post(self, command):
        if self.closed or command.get("action") not in ("queue", "lyrics", "fullscreen", "select"):
            return OperationResult(S.UNAVAILABLE, "播放页面已经关闭或尚未就绪")
        try:
            self.requested.emit(command)
        except RuntimeError:
            self.closed = True
            return OperationResult(S.UNAVAILABLE, "播放页面已经关闭")
        return OperationResult(S.ACCEPTED, "已请求打开对应的播放工具")

    @Slot(object)
    def _apply(self, command):
        if self.closed or command.is_current is not None and not command.is_current():
            return
        music = self.tabs._panels.get("music")
        movie = self.tabs._panels.get("movie")
        action = command.get("action")
        with operation_scope(command.notification_id):
            try:
                if action == "queue" and music is not None:
                    music._show_queue()
                    notify("播放队列已打开")
                elif action == "lyrics" and music is not None:
                    enabled = command.get("enabled") is True
                    music.btn_lyrics.setChecked(enabled)
                    music._on_toggle_lyrics(enabled)
                    window = getattr(music, "_lyrics_window", None)
                    if enabled and (window is None or not window.isVisible()):
                        notify("歌词暂未打开", "请先选择一首歌曲", status="warning")
                    else:
                        notify("歌词已打开" if enabled else "歌词已收起")
                elif action == "fullscreen" and movie is not None and movie._movie.is_playing():
                    window = movie._video_win
                    enabled = command.get("enabled") is True
                    if window is not None:
                        window.request_fullscreen() if enabled else window.showNormal()
                    else:
                        movie._movie.set_fullscreen(enabled)
                    notify("已请求全屏" if enabled else "已请求退出全屏", status="warning")
                elif action == "select" and movie is not None and not movie._recommendation_closed:
                    row = movie._movie.voice_candidate_row(command.get("candidate"))
                    if row is None:
                        notify("推荐候选已经变化", "请查看当前列表后重新选择", status="warning")
                        return
                    if row.get("_resume_item"):
                        movie._resume_recommended(row, command.is_current)
                    else:
                        movie._find_discovery_work(row)
                    notify("已选择候选", "正在查找片源；选择可用片源后才会播放", status="warning")
                else:
                    notify("播放工具尚未就绪", "请先打开音乐或电影页面", status="warning")
            except RuntimeError:
                notify("页面已关闭", "请重新打开对应的播放页面", status="warning")
