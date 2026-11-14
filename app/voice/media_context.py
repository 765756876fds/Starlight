"""Live, bounded media arbitration. No Qt access, utterance memory or background service."""
from dataclasses import dataclass
import threading
import time

from ..core.command_bus import Command, CommandType
from ..core.operation_result import OperationResult, OperationStatus


@dataclass(frozen=True)
class MediaSample:
    domain: str
    state: str
    token: tuple = ()


class MediaContext:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self._lock = threading.Lock()
        self._focus = None
        self._paused = None

    def _controller_token(self, domain):
        provider = getattr(self.runtime, domain + "_controller_provider", None)
        controller = provider() if callable(provider) else None
        generation = getattr(controller, "_play_request_id" if domain == "music" else "_playback_gen", None)
        return id(controller), generation

    def note_focus(self, domain):
        # Called by the GUI event filter; controller integers only, never IPC/Qt getters here.
        with self._lock:
            self._focus = (domain, self.clock(), self._controller_token(domain)) if domain in ("music", "movie") else None

    def sample(self, domain):
        provider = getattr(self.runtime, domain + "_mpv_provider", None)
        try:
            mpv = provider() if callable(provider) else None
            if mpv is None or mpv.is_running() is False:
                return MediaSample(domain, "stopped")
            if mpv.is_running() is not True:
                return MediaSample(domain, "unknown")
            idle = mpv.get_property("idle-active", timeout=.15)
            if idle is True:
                return MediaSample(domain, "stopped")
            path = mpv.get_property("path", timeout=.15)
            paused = mpv.get_property("pause", timeout=.15)
            if idle is not False or not isinstance(path, str) or not path or type(paused) is not bool:
                return MediaSample(domain, "unknown")
            token = (id(mpv), *self._controller_token(domain), path)
            return MediaSample(domain, "paused" if paused else "playing", token)
        except Exception:
            return MediaSample(domain, "unknown")

    def execute(self, action, *, is_current=None):
        current = is_current or (lambda: True)
        if action not in ("pause", "resume", "next"):
            return OperationResult(OperationStatus.NEEDS_PARAMETER, "请说明要暂停、继续还是切换")
        samples = [self.sample(d) for d in ("music", "movie")]
        if not current():
            return OperationResult(OperationStatus.UNKNOWN, "已取消过时的播放操作")
        state = "paused" if action == "resume" else "playing"
        candidates = [sample for sample in samples if sample.state == state]
        if action == "next" and not candidates:
            candidates = [sample for sample in samples if sample.state == "paused"]
        selected = None
        with self._lock:
            focus, paused = self._focus, self._paused
        if focus and self.clock() - focus[1] <= 30:
            selected = next((s for s in candidates if s.domain == focus[0]
                             and s.token[1:3] == focus[2]), None)
        if selected is None and action == "resume" and paused and self.clock() - paused[1] <= 300:
            selected = next((s for s in candidates if s.token == paused[0]), None)
        if selected is None and len(candidates) == 1 and all(s.state != "unknown" for s in samples):
            selected = candidates[0]
        if selected is None:
            message = "请明确说音乐还是电影" if candidates else "当前没有可确认的播放内容，请先选择歌曲或影片"
            return OperationResult(OperationStatus.NEEDS_PARAMETER, message)
        if action == "next":
            if selected.domain == "movie":
                return OperationResult(OperationStatus.NEEDS_PARAMETER, "想换一部还是下一集？请说下一集，或让我推荐电影")
            provider = getattr(self.runtime, "music_controller_provider", None)
            controller = provider() if callable(provider) else None
            queue = getattr(controller, "queue", None)
            if queue is None or queue.size() < 1:
                return OperationResult(OperationStatus.UNAVAILABLE, "当前没有明确的音乐播放集合")
            command = CommandType.MUSIC_NEXT
        else:
            command = {("music", "pause"): CommandType.MUSIC_PAUSE,
                       ("music", "resume"): CommandType.MUSIC_RESUME,
                       ("movie", "pause"): CommandType.MOVIE_PAUSE,
                       ("movie", "resume"): CommandType.MOVIE_RESUME}[(selected.domain, action)]
        latest = [self.sample(d) for d in ("music", "movie")]
        with self._lock:
            focus_changed = self._focus != focus
        if not current() or latest != samples or focus_changed:
            return OperationResult(OperationStatus.UNKNOWN, "播放内容已变化，请重新说一次")
        if not self.runtime.command_bus.has_handler(command):
            return OperationResult(OperationStatus.UNAVAILABLE, "播放器尚未就绪，请稍后再试")
        value = self.runtime.command_bus.execute(Command(command, source="voice", is_current=current))
        if value is False:
            return OperationResult(OperationStatus.FAILED, "播放操作未成功，请检查播放器")
        if action == "next":
            return OperationResult(OperationStatus.ACCEPTED, "正在切换当前播放集合的下一首")
        after = self.sample(selected.domain)
        if not current() or after.token != selected.token:
            return OperationResult(OperationStatus.UNKNOWN, "播放内容已变化，无法确认本次操作")
        if after.state != ("paused" if action == "pause" else "playing"):
            return OperationResult(OperationStatus.UNKNOWN, "暂时无法确认播放器状态，请稍后再试")
        if action == "pause":
            with self._lock:
                self._paused = (after.token, self.clock())
        label = "音乐" if selected.domain == "music" else "电影"
        return OperationResult(OperationStatus.CONFIRMED, label + ("已暂停" if action == "pause" else "已继续播放"))
