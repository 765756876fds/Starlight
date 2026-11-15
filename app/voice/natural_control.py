"""Natural media decisions and guarded existing CommandBus execution.

No inference engine or Qt imports. Private memory is optional and non-blocking.
"""

from dataclasses import fields, replace
from pathlib import Path
import threading
import time
import uuid
import math

from ..core.command_bus import Command, CommandType as C
from ..core.operation_result import OperationResult, OperationStatus as S
from ..music.preferences import media_key
from .interaction_memory import InteractionMemory
from .natural_language import ACTIONS, Meaning


OK = (S.CONFIRMED, S.ACCEPTED)


class NaturalControl:
    def __init__(self, runtime, memory=None):
        self.runtime = runtime
        if memory is None:
            from ..core.path_resolve import USER_DATA_DIR

            memory = InteractionMemory(Path(USER_DATA_DIR) / "data/interaction_memory.db")
        self.memory = memory
        self._lock = threading.RLock()
        self._last = None
        self._closed = False
        self.memory.start()
        self.bind()

    def controller(self, domain):
        provider = getattr(self.runtime, domain + "_controller_provider", None)
        return provider() if callable(provider) else None

    def bind(self):
        bus = self.runtime.command_bus
        bus.register(C.NATURAL_MEDIA, self.execute)
        for domain, name in (("music", "music_memory"), ("movie", "film_memory")):
            memory = getattr(self.controller(domain), name, None)
            if memory is not None:
                memory.weak_provider = lambda d=domain: self.memory.weak_profile(d)

    def close(self):
        self._closed = True
        for domain, name in (("music", "music_memory"), ("movie", "film_memory")):
            memory = getattr(self.controller(domain), name, None)
            if memory is not None:
                memory.weak_provider = None
        return self.memory.close()

    def _bus(self, command, params, current):
        if not current() or self._closed:
            return OperationResult(S.UNKNOWN, "这次操作已过期，请重新说一次")
        if not self.runtime.command_bus.has_handler(command):
            return OperationResult(S.UNAVAILABLE, "这项功能尚未就绪，请先打开对应页面")
        try:
            result = self.runtime.command_bus.execute(
                Command(command, params=params, source="voice", is_current=current)
            )
        except TimeoutError:
            return OperationResult(S.TIMEOUT, "这次操作超时，请稍后重试")
        except Exception:
            return OperationResult(S.FAILED, "这次操作没有完成，当前播放仍可手动控制")
        if isinstance(result, OperationResult):
            return result
        return OperationResult(
            S.CONFIRMED if result is True else S.FAILED if result is False else S.UNKNOWN,
            "操作已完成"
            if result is True
            else "操作未完成，请重试"
            if result is False
            else "操作结果尚未确认，请查看播放页面",
        )

    @staticmethod
    def generation(controller, domain):
        return (
            id(controller),
            getattr(controller, "_play_request_id" if domain == "music" else "_playback_gen", None),
            getattr(controller, "_current_index", None) if domain == "movie" else None,
        )

    def _frozen_song(self, controller):
        lock = getattr(controller, "_lock", None)
        if lock is None or not lock.acquire(timeout=0.15):
            return None
        try:
            song = controller.get_current_song()
            return (
                dict(song)
                if isinstance(song, dict)
                and getattr(controller, "_is_switching_track", False) is not True
                else None
            )
        finally:
            lock.release()

    def _previous(self, request, *, undo=False):
        with self._lock:
            last = self._last
        if (
            not last
            or request != last["request"] + 1
            or time.monotonic() - last["at"] > 30
            or self.generation(self.controller(last["domain"]), last["domain"]) != last["after"]
        ):
            return None
        if undo and last["weak_id"]:
            self.memory.revoke(last["weak_id"])
        return last

    def _remember_action(self, meaning, domain, request, weak_id="", parent=None):
        controller = self.controller(domain)
        action_id = uuid.uuid4().hex
        self.memory.record(
            action_id,
            feature=parent["feature"] if parent else meaning.feature,
            domain=domain,
            action=meaning.action,
            kind="correction" if meaning.correction else "observation",
            parent=parent["weak_id"] if parent else "",
        )
        with self._lock:
            self._last = dict(
                domain=domain,
                request=request,
                at=time.monotonic(),
                after=self.generation(controller, domain),
                feature=meaning.feature,
                action=meaning.action,
                weak_id=weak_id,
                action_id=action_id,
            )

    def reject_previous(self, request, rejected):
        last = self._previous(request)
        if last is None or not last["weak_id"] or last["action"] != "evaluate":
            return False
        expected = "next" if last["domain"] == "music" else "stop"
        if rejected.action != expected or rejected.target not in ("current", last["domain"]):
            return False
        if not self.memory.revoke(last["weak_id"]):
            return False
        self.memory.record(
            uuid.uuid4().hex,
            feature=last["feature"],
            domain=last["domain"],
            action="correction_rejection",
            kind="observation",
            parent=last["weak_id"],
        )
        with self._lock:
            if self._last is last:
                self._last = None
        return True

    def _film_kind(self, movie, explicit=""):
        if explicit in ("movie", "tv", "anime", "variety"):
            return explicit
        browsed = getattr(movie, "voice_browse_kind", "")
        if browsed in ("movie", "tv", "anime", "variety"):
            return browsed
        memory = getattr(movie, "film_memory", None)
        candidates = []
        if memory is not None:
            for kind in ("movie", "tv", "anime"):
                candidates += [
                    (row.get("updated", 0), kind)
                    for row in memory.records(kind)
                    if row.get("meaningful")
                ]
        return max(candidates)[1] if candidates else "mixed"

    def execute(self, command):
        try:
            if set(command.params) - {f.name for f in fields(Meaning)}:
                raise ValueError("unexpected natural parameters")
            meaning = Meaning(**command.params)
            if meaning.action not in ACTIONS or meaning.target not in ("current", "music", "movie"):
                raise ValueError("unsupported semantic candidate")
            if (
                meaning.rating not in ("", "positive", "negative", "neutral")
                or type(meaning.save) is not bool
                or type(meaning.correction) is not bool
                or meaning.period not in ("", "yesterday")
                or meaning.kind not in ("", "movie", "tv", "anime", "variety")
            ):
                raise ValueError("invalid semantic fields")
            if (meaning.rating and meaning.action != "evaluate") or (
                meaning.action == "evaluate" and not meaning.rating
            ):
                raise ValueError("evaluation requires an explicit rating concept")
            if meaning.save and (
                meaning.action not in ("save", "evaluate")
                or meaning.rating in ("negative", "neutral")
            ):
                raise ValueError("unsupported combined actions")
            if meaning.action == "save" and not meaning.save:
                raise ValueError("save requires a save concept")
            if meaning.value is not None and (
                type(meaning.value) not in (int, float) or not math.isfinite(meaning.value)
            ):
                raise ValueError("invalid numeric field")
            if (
                meaning.action == "select"
                and meaning.value is not None
                and (not 1 <= meaning.value <= 20 or int(meaning.value) != meaning.value)
            ):
                raise ValueError("invalid candidate index")
            if (
                meaning.action == "speed"
                and meaning.value is not None
                and not 0.25 <= meaning.value <= 4
            ):
                raise ValueError("invalid playback speed")
            meaning = replace(
                meaning,
                feature=":".join(
                    (meaning.action, meaning.target, meaning.rating, "save" if meaning.save else "")
                ),
            )
        except (TypeError, ValueError):
            return OperationResult(S.UNSUPPORTED, "请换一种说法，一次说一个操作")
        current = command.is_current or (lambda: True)
        if not current() or self._closed:
            return OperationResult(S.UNKNOWN, "已忽略过时的操作")
        self.bind()
        request = getattr(self.runtime, "_voice_request_generation", 0)
        if meaning.action == "confirm":
            last = self._previous(request)
            if last is None:
                return OperationResult(
                    S.NEEDS_PARAMETER, "没有可可靠关联的上次操作，请直接说你的要求"
                )
            self.memory.record(
                "confirm:" + last["action_id"],
                feature=last["feature"],
                domain=last["domain"],
                action=last["action"],
                kind="confirmation",
                parent=last["action_id"],
            )
            with self._lock:
                self._last["request"] = request
            return OperationResult(S.ACCEPTED, "好的，正在记住这次确认")
        parent = self._previous(request, undo=True) if meaning.correction else None
        context = self.runtime.media_context
        action = meaning.action
        if action in ("pause", "resume"):
            result = context.execute(
                action,
                hint=meaning.target,
                feature=meaning.feature,
                memory=self.memory,
                is_current=current,
            )
            if result.status is S.CONFIRMED:
                domain = "music" if result.message.startswith("音乐") else "movie"
                self._remember_action(meaning, domain, request, parent=parent)
            return result
        if action in ("queue", "lyrics", "fullscreen", "select"):
            if action == "select":
                movie = self.controller("movie")
                capture = getattr(movie, "voice_candidate", None)
                candidate = (
                    capture(int(meaning.value) - 1 if meaning.value is not None else None)
                    if callable(capture)
                    else None
                )
                if not candidate:
                    return OperationResult(S.NEEDS_PARAMETER, "请先查看当前推荐候选，再说第几部")
                return self._bus(C.MEDIA_UI, {"action": "select", "candidate": candidate}, current)
            domain = "movie" if action == "fullscreen" else "music"
            token = self.generation(self.controller(domain), domain)

            def ui_current():
                return current() and self.generation(self.controller(domain), domain) == token

            return self._bus(
                C.MEDIA_UI, {"action": action, "enabled": meaning.value != 0}, ui_current
            )
        if action in ("recommend", "continue_watch"):
            domain = meaning.target if meaning.target != "current" else context.page_hint()
            if action == "continue_watch":
                domain = "movie"
            if domain not in ("music", "movie"):
                return OperationResult(S.NEEDS_PARAMETER, "想听音乐还是看影视？请告诉我")
            if domain == "movie":
                movie = self.controller("movie")
                kind = meaning.kind or (
                    "mixed" if action == "continue_watch" else self._film_kind(movie)
                )
                params = {"kind": kind}
                if getattr(meaning, "period", ""):
                    params["period"] = meaning.period
                result = self._bus(
                    C.MOVIE_CONTINUE if action == "continue_watch" else C.MOVIE_RECOMMEND,
                    params,
                    current,
                )
            else:
                result = self._bus(C.MUSIC_RECOMMEND, {}, current)
            if result.status in OK:
                self._remember_action(meaning, domain, request, parent=parent)
            return result
        selecting = (
            "positive"
            if action
            in (
                "save",
                "subtitle",
                "speed",
                "volume_up",
                "volume_down",
                "seek_forward",
                "seek_backward",
                "stop",
            )
            or meaning.rating == "positive"
            else "next"
            if action in ("next", "previous", "style", "chart")
            else "pause"
        )
        selected, samples, focus = context.select(
            selecting, meaning.target, feature=meaning.feature, memory=self.memory
        )
        if selected is None:
            return OperationResult(S.NEEDS_PARAMETER, "没有唯一可靠的播放对象，请明确说音乐或电影")
        domain = selected.domain
        controller = self.controller(domain)
        generation = self.generation(controller, domain)
        song = self._frozen_song(controller) if domain == "music" else None
        item = (
            dict(controller.current() or {})
            if domain == "movie" and controller is not None
            else None
        )
        if domain == "music" and not song:
            return OperationResult(S.UNKNOWN, "歌曲正在切换或尚未就绪，请稍后再试")

        def guard():
            if (
                not current()
                or self._closed
                or self.generation(self.controller(domain), domain) != generation
            ):
                return False
            if domain == "music":
                actual = controller.get_current_song()
                return isinstance(actual, dict) and media_key(actual) == media_key(song)
            return controller.current() == item

        latest = [context.sample(d) for d in ("music", "movie")]
        with context._lock:
            focus_changed = context._focus != focus and meaning.target == "current"
        if latest != samples or focus_changed or not guard():
            return OperationResult(S.UNKNOWN, "播放内容已变化，请重新说一次")
        weak_id = ""
        if action in ("evaluate", "save"):
            if domain == "music":
                if meaning.rating in ("negative", "neutral"):
                    if getattr(controller, "queue", None) is None or controller.queue.size() < 1:
                        return OperationResult(S.UNAVAILABLE, "当前没有可切换的音乐队列")
                    result = self._bus(C.MUSIC_GUARDED_ACTION, {"action": "next"}, guard)
                    if result.status in OK and current():
                        weak_id = uuid.uuid4().hex
                        saved = self.memory.record(
                            weak_id,
                            feature=meaning.feature,
                            domain="music",
                            action="next",
                            media_key=media_key(song),
                            rating=meaning.rating,
                            kind="weak",
                        )
                        result = OperationResult(
                            result.status,
                            result.message
                            + ("，正在保存这首歌的轻微反馈" if saved else "，偏好暂未保存"),
                        )
                else:
                    result = (
                        self._bus(C.MUSIC_FEEDBACK, {"song": song}, guard)
                        if meaning.rating
                        else OperationResult(S.CONFIRMED, "")
                    )
                    if meaning.rating and result.status in OK:
                        self.memory.record(
                            uuid.uuid4().hex,
                            feature=meaning.feature,
                            domain="music",
                            action="like",
                            media_key=media_key(song),
                            rating="positive",
                            kind="explicit",
                        )
                    if meaning.save and guard():
                        saved = self._bus(C.MUSIC_SAVE_CURRENT, {"song": song}, guard)
                        if saved.status in OK and result.status not in OK:
                            result = OperationResult(saved.status, saved.message + "；偏好暂未保存")
                        elif saved.status not in OK and result.status in OK and meaning.rating:
                            result = OperationResult(saved.status, "已记住喜欢；" + saved.message)
                        else:
                            result = saved
            elif meaning.rating == "neutral":
                from ..movie.recommendation import item_work
                from ..movie.preferences import work_key

                weak_id = uuid.uuid4().hex
                saved = self.memory.record(
                    weak_id,
                    feature=meaning.feature,
                    domain="movie",
                    action="evaluate",
                    media_key=work_key(item_work(item)),
                    rating="neutral",
                    kind="weak",
                )
                result = OperationResult(
                    S.ACCEPTED if saved else S.UNAVAILABLE,
                    "正在保存轻微评价，电影继续播放" if saved else "电影继续播放，评价暂未保存",
                )
            elif meaning.rating == "negative":
                from ..movie.recommendation import item_work
                from ..movie.preferences import work_key

                work = item_work(item)
                stopped = self._bus(C.MOVIE_STOP, {}, guard)
                if stopped.status not in OK:
                    return stopped
                weak_id = uuid.uuid4().hex
                self.memory.record(
                    weak_id,
                    feature=meaning.feature,
                    domain="movie",
                    action="replace",
                    media_key=work_key(work),
                    rating=meaning.rating,
                    kind="weak",
                )
                stopped_generation = self.generation(controller, domain)
                result = self._bus(
                    C.MOVIE_RECOMMEND,
                    {"kind": self._film_kind(controller, work.get("kind"))},
                    lambda: (
                        current()
                        and self.generation(controller, domain) == stopped_generation
                        and not controller.current()
                    ),
                )
                if result.status not in OK:
                    result = OperationResult(
                        result.status, "电影已停止，进度沿用原有保存流程；替代推荐暂未完成，请重试"
                    )
            elif meaning.save:
                return OperationResult(S.UNSUPPORTED, "影视收藏尚未接通，请在电影页面选择内容")
            else:
                result = self._bus(C.MOVIE_FEEDBACK, {"item": item, "positive": True}, guard)
        elif action in ("style", "chart"):
            if domain != "music":
                return OperationResult(S.NEEDS_PARAMETER, "请说明音乐风格，或让我推荐另一部影视")
            params = {"change_chart": True}
            if action == "style":
                params["change_style"] = True
            params["expected_request"] = generation[1]
            # The recommender owns the next playback generation. Its external
            # guard must not invalidate its own successful load/queue callback.
            result = (
                self._bus(C.MUSIC_RECOMMEND, params, current)
                if guard()
                else OperationResult(S.UNKNOWN, "播放已变化")
            )
        elif action in ("next", "previous", "stop"):
            if domain != "music":
                return OperationResult(S.NEEDS_PARAMETER, "请说下一集或让我推荐另一部电影")
            result = self._bus(C.MUSIC_GUARDED_ACTION, {"action": action}, guard)
        elif action == "subtitle":
            if domain != "movie":
                return OperationResult(S.NEEDS_PARAMETER, "请先打开电影")
            state = controller.subtitle_state()
            if not state or not guard():
                return OperationResult(S.UNKNOWN, "当前视频尚未就绪，请稍后再试")
            ok, message = controller.set_subtitle_visible(
                meaning.value != 0, expected_media=state["media"]
            )
            result = OperationResult(
                S.CONFIRMED if ok is True else S.UNKNOWN,
                message,
            )
        elif action == "speed":
            if meaning.value is None:
                return OperationResult(S.NEEDS_PARAMETER, "请说具体倍速，例如电影一点五倍速")
            if domain == "music":
                result = self._bus(C.MUSIC_SET_SPEED, {"speed": meaning.value}, guard)
            else:
                controller.set_speed(meaning.value)
                actual = controller.mpv.get_property("speed", timeout=0.15)
                result = OperationResult(
                    S.CONFIRMED
                    if type(actual) in (int, float) and abs(actual - meaning.value) < 0.01
                    else S.UNKNOWN,
                    "电影倍速已设置" if actual == meaning.value else "电影倍速暂未确认",
                )
        else:
            command_type = domain + "." + ("seek" if action.startswith("seek_") else action)
            if action.startswith("seek_"):
                if type(meaning.value) not in (int, float) or not 1 <= meaning.value <= 60:
                    return OperationResult(S.NEEDS_PARAMETER, "请说明快进或后退一到六十秒")
                params = {
                    "seconds": meaning.value * (-1 if action == "seek_backward" else 1),
                    "mode": "relative",
                }
            else:
                params = {"step": 5}
            result = self._bus(command_type, params, guard)
        if result.status in OK and current():
            self._remember_action(meaning, domain, request, weak_id, parent)
        return result
