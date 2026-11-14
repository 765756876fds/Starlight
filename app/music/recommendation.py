"""Bounded chart recommendation on existing sources, queue and MPV. No new worker pool."""
import random
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from app.core.operation_result import OperationResult, OperationStatus
from .preferences import media_key, normalize


def source_revision(manager):
    lock = getattr(manager, "_life_lock", None)
    if lock is None:
        return ()
    with lock:
        return tuple(sorted(getattr(manager, "_source_versions", {}).items()))

STYLE_TERMS = {
    "伤感": {"伤感": 1, "伤心": .9, "失恋": .8, "抒情": .4},
    "轻松": {"轻松": 1, "治愈": .7, "休闲": .6},
    "摇滚": {"摇滚": 1, "rock": .9},
    "怀旧": {"怀旧": 1, "经典": .7, "老歌": .8},
    "动感": {"动感": 1, "舞曲": .8, "电音": .6},
}


def style_score(title, style):
    text = normalize(title)
    return max((w for term, w in STYLE_TERMS.get(style, {style: 1}).items()
                if term and normalize(term) in text), default=0)


def track_row(track):
    return {"title": track.title, "artist": track.artist, "album": track.album,
            "duration": track.duration_seconds, "source_type": "online", "source": "online",
            "source_name": track.source_name, "track_id": track.track_id,
            "extra": dict(track.extra or {}), "id": 0, "path": "", "filename": ""}


def known_tags(track):
    """Only provider-supplied fields; never infer language/decade from a singer's name."""
    tags = {"language": normalize(track.extra.get("language"))}
    year = track.extra.get("year")
    if str(year or "").isdigit() and 1900 <= int(year) <= 2100:
        tags["era"] = str(int(year) // 10 * 10)
    return tags


@dataclass(frozen=True)
class Choice:
    chart: dict
    tracks: list
    index: int
    reason: str


class RecommendationService:
    MAX_CHARTS = 3
    MAX_TRIES = 4
    READ_TIMEOUT = 20

    def _read(self, manager, read, current):
        """Use the source manager's existing bounded pool; never add a permanent worker."""
        executor = getattr(manager, "_executor", None)
        if executor is None:
            return read()  # Simple in-process source fixtures; production manager owns the pool.
        future = executor.submit(read)
        end = time.monotonic() + self.READ_TIMEOUT
        try:
            while current() and time.monotonic() < end:
                try:
                    return future.result(timeout=min(.1, max(.001, end - time.monotonic())))
                except FutureTimeout:
                    if future.done():
                        raise
            raise TimeoutError("recommendation source deadline")
        finally:
            future.cancel()  # Running reads remain bounded by the existing JS bridge timeout.

    def __init__(self, controller, memory, rng=None):
        self.controller, self.memory = controller, memory
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._busy = False
        self.session = None
        self._catalog = None
        self._catalog_at = 0

    def cancel(self, close=False, clear_session=False):
        with self._lock:
            self._generation += 1
            self._closed |= close
            if clear_session or close:
                self.session = None

    def _current(self, generation, guard):
        if guard is not None and not guard():
            return False
        with self._lock:
            return not self._closed and self._generation == generation

    def choices(self, manager, style="", change_chart=False, change_style=False, current=lambda: True):
        cache_key = (id(manager), source_revision(manager))
        if self._catalog and self._catalog[0] == cache_key and time.monotonic() - self._catalog_at < 120:
            catalog = self._catalog[1]
        else:
            catalog = self._read(manager, manager.get_top_lists, current)
            if catalog:
                self._catalog = (cache_key, catalog)
                self._catalog_at = time.monotonic()
        profile = self.memory.profile()
        explicit = profile["explicit"]
        charts = []
        for source, groups in (catalog or {}).items():
            for group in groups or []:
                for item in group.get("lists", []):
                    if not item.get("id") or not item.get("title"):
                        continue
                    chart = dict(item, source=source)
                    text = str(group.get("title", "")) + " " + chart["title"]
                    if (change_chart or change_style) and self.session:
                        if (source, str(item["id"])) == self.session["key"]:
                            continue
                    if style and not style_score(text, style):
                        continue  # Never silently ignore an explicit style request.
                    if change_style and self.session and self.session.get("style"):
                        if style_score(text, self.session["style"]):
                            continue
                    matches = sum(weight * style_score(text, value)
                                  for (kind, value), weight in explicit.items() if kind == "style")
                    if not style and any(weight < 0 and style_score(text, value)
                                         for (kind, value), weight in explicit.items() if kind == "style"):
                        continue
                    if style:
                        matches = style_score(text, style)
                    chart["preference"] = matches
                    chart["popular"] = any(word in text.casefold() for word in
                        ("热门", "热歌", "热度", "飙升", "流行", "top", "hot"))
                    charts.append(chart)
        self.rng.shuffle(charts)
        charts.sort(key=lambda c: (c["preference"], c["popular"]), reverse=True)
        self.memory.cache("chart_labels", [{k: c[k] for k in ("source", "id", "title")} for c in charts[:100]])
        options = []
        # Round-robin providers before a second chart from a failed provider.
        buckets = {}
        for chart in charts:
            buckets.setdefault(chart["source"], []).append(chart)
        selected_charts = []
        while buckets and len(selected_charts) < self.MAX_CHARTS:
            for source in list(buckets):
                selected_charts.append(buckets[source].pop(0))
                if not buckets[source]:
                    del buckets[source]
                if len(selected_charts) == self.MAX_CHARTS:
                    break
        for chart in selected_charts:
            if not current():
                break
            try:
                detail = self._read(manager,
                    lambda chart=chart: manager.get_top_list_detail(chart["source"], str(chart["id"]), page=1), current)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("music_recommendation_chart_failed")
                continue
            tracks = list((detail or {}).get("tracks", []))[:50]
            for index, track in enumerate(tracks[:20]):
                artist = normalize(track.artist)
                key = media_key(track_row(track))
                features = dict(known_tags(track), artist=artist, track=key)
                if any(value and explicit.get((kind, value), 0) < 0 for kind, value in features.items()):
                    continue
                explicit_match = sum(max(0, explicit.get((kind, value), 0))
                                     for kind, value in features.items() if value)
                tier = (chart["preference"], explicit_match, profile["artists"].get(artist, 0))
                reason = "按本次“%s”要求匹配榜单" % style if style else (
                    "按你设置的榜单风格偏好" if chart["preference"] else
                    "根据你明确设置且有资料匹配的偏好" if explicit_match else
                    "参考你的主动收听记录" if tier[2] else "从可用榜单前列选择")
                if change_style and self.session and not self.session.get("style"):
                    reason = "当前风格资料不足，改选另一榜单；" + reason
                options.append((tier, index, Choice(chart, tracks, index, reason)))
        if not options:
            return []
        # Preference determines the pool; rank determines weight inside it, never raw platform heat.
        pool = list(options)
        ordered = []
        seen = set()
        while pool and len(ordered) < self.MAX_TRIES:
            tier = max(item[0] for item in pool)
            leading = [item for item in pool if item[0] == tier]
            selected = self.rng.choices(leading, weights=[1 / (1 + item[1]) ** 1.5 for item in leading])[0]
            pool.remove(selected)
            identity = media_key(track_row(selected[2].tracks[selected[2].index]))
            if identity not in seen:
                seen.add(identity)
                ordered.append(selected[2])
        return ordered

    def play(self, *, style="", change_chart=False, change_style=False, is_current=None):
        with self._lock:
            if self._closed or self._busy:
                return OperationResult(OperationStatus.UNAVAILABLE, "推荐仍在处理中，请稍候再试")
            self._busy = True
            self._generation += 1
            generation = self._generation
        controller = self.controller
        original_request = controller._play_request_id
        manager = controller.online_manager
        revision = source_revision(manager)
        deadline = time.monotonic() + 45
        def current():
            return (self._current(generation, is_current) and time.monotonic() < deadline
                    and controller.online_manager is manager and source_revision(manager) == revision)
        try:
            if controller.online_manager is None or not controller._mpv_available:
                return OperationResult(OperationStatus.UNAVAILABLE, "音源或播放器尚未就绪，请检查音乐设置")
            self.memory.import_history_once(controller.db.get_history(limit=100))
            try:
                choices = self.choices(controller.online_manager, style, change_chart, change_style, current)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("music_recommendation_catalog_failed")
                return OperationResult(OperationStatus.FAILED, "榜单暂时无法获取，请重试或切换音源")
            if not current():
                return OperationResult(OperationStatus.UNKNOWN, "推荐已取消或超时，请重试")
            for choice in choices:
                if not current() or controller._play_request_id != original_request:
                    return OperationResult(OperationStatus.UNKNOWN, "已取消过时的推荐")
                track = choice.tracks[choice.index]
                try:
                    result = self._read(manager, lambda track=track: manager.get_play_url(track), current)
                except Exception:
                    continue
                if not result or not result.get("url"):
                    continue

                def loaded():
                    collection = choice.tracks[choice.index:] + choice.tracks[:choice.index]
                    remaining = [track_row(t) for t in collection]
                    for row in remaining:
                        row["extra"]["_starlight_recommended"] = True
                    controller.queue.clear()
                    controller.queue.add_all(remaining)
                    controller.queue.play_at(0, collection_start=True)
                    self.session = {"key": (choice.chart["source"], str(choice.chart["id"])),
                                    "title": choice.chart["title"], "source": choice.chart["source"],
                                    "reason": choice.reason, "style": style}

                if controller.play_online(track, _before_load=current, _on_loaded=loaded,
                                          _resolved=result, _expected_request=original_request,
                                          _recommendation=True):
                    # IPC acknowledgement alone is not proof of decoded audio.
                    playback_deadline = time.monotonic() + 2
                    while current() and time.monotonic() < playback_deadline:
                        with controller._lock:
                            if (controller._play_request_id != original_request + 1
                                    or controller._current_song is None
                                    or controller._current_song.get("track_id") != track.track_id):
                                return OperationResult(OperationStatus.UNKNOWN, "播放已切换，推荐结果不再更新")
                        try:
                            path = controller.mpv.get_property("path", timeout=.2)
                            position = controller.mpv.get_property("time-pos", timeout=.2)
                            idle = controller.mpv.get_property("idle-active", timeout=.2)
                            if path == result["url"] and position is not None and idle is False:
                                return OperationResult(OperationStatus.CONFIRMED,
                                    f"正在播放：{track.title} · {choice.chart['source']} / {choice.chart['title']}；{choice.reason}"
                                    + ("；偏好暂时无法保存，本次仍可播放" if not self.memory.available else ""))
                        except Exception:
                            break
                        time.sleep(.1)
                    if not current():
                        return OperationResult(OperationStatus.UNKNOWN, "播放已切换，推荐结果不再更新")
                    return OperationResult(OperationStatus.ACCEPTED, "已交给播放器加载，尚未确认起播；可在音乐页查看或换源")
                # Only our own failed IPC attempt may advance this bounded fallback.
                with controller._lock:
                    if not current() or controller._play_request_id != original_request + 1:
                        return OperationResult(OperationStatus.UNKNOWN, "已取消过时的推荐")
                    original_request += 1
            return OperationResult(OperationStatus.FAILED,
                "没有找到符合要求的可播放歌曲，请换个风格或音源后重试" if style else
                "可用榜单中暂时没有可播放歌曲，请换个音源后重试")
        finally:
            with self._lock:
                self._busy = False
