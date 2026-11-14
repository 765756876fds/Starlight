"""Film discovery, preferences and resume candidates; never resolves playback URLs."""
from dataclasses import dataclass, field
from copy import deepcopy
import random
import threading

from . import douban_rank
from .cms_parser import episode_eps
from .discovery import CATEGORIES
from .library import title_key
from .preferences import KINDS, work_key, work_tags

GENRES = ("悬疑", "科幻", "喜剧", "动作", "爱情", "恐怖", "剧情", "动画", "纪录片")


def tags(work):
    return work_tags(work)


def matching_record(work, records):
    key = work_key(work)
    for record in records:
        old = record["work"]
        if record["key"] == key:
            return record
        if old.get("kind") != work.get("kind") or title_key(old.get("title", "")) != title_key(work.get("title", "")):
            continue
        if any(old.get(k) is not None and work.get(k) is not None and old[k] != work[k] for k in ("year", "season")):
            continue
        old_ids = {(s.get("platform"), str(s["source_id"])) for s in old.get("sources", []) if s.get("source_id")}
        ids = {(s.get("platform"), str(s["source_id"])) for s in work.get("sources", []) if s.get("source_id")}
        complete = (type(work.get("year")) is int and old.get("year") == work["year"]
                    and bool(work.get("region")) and old.get("region") == work["region"])
        if work.get("kind") != "movie":
            complete = complete and type(work.get("season")) is int and old.get("season") == work["season"]
        if old_ids & ids or complete:
            return record
    return None


def item_work(item):
    attached = item.get("_starlight_work")
    if isinstance(attached, dict) and attached.get("kind") in KINDS:
        return deepcopy(attached)
    explicit = str(item.get("type_name") or item.get("vod_type") or "")
    kind = next((k for word, k in (("动漫", "anime"), ("动画", "anime"), ("电视剧", "tv"),
                ("剧集", "tv"), ("综艺", "variety"), ("电影", "movie")) if word in explicit), "")
    year = item.get("vod_year")
    return dict(title=str(item.get("vod_name") or ""), kind=kind,
                year=int(year) if str(year or "").isdigit() and 1800 <= int(year) <= 2200 else None,
                region=str(item.get("vod_area") or ""),
                genres=[v for v in GENRES if v in str(item.get("vod_class") or "")],
                sources=[dict(platform=str(item.get("source_name") or "playback"),
                              platform_name=str(item.get("source_name") or "播放记录"),
                              source_id=str(item.get("vod_id") or ""))])


@dataclass
class FilmBatch:
    rows: list = field(default_factory=list)
    message: str = ""
    kind: str = "movie"
    action: str = "recommend"
    states: list = field(default_factory=list)


class FilmRecommendations:
    def __init__(self, movie, memory, discovery, metadata=None, rng=None):
        self.movie, self.memory, self.discovery = movie, memory, discovery
        self.metadata = metadata or douban_rank.get_subjects
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False

    def cancel(self, *, close=False):
        with self._lock:
            self._generation += 1
            self._closed |= close

    def begin(self, guard=None):
        with self._lock:
            self._generation += 1
            generation = self._generation
        def current():
            with self._lock:
                valid = not self._closed and generation == self._generation
            return valid and (guard is None or guard())
        return current

    def recommend(self, kind="movie", genre="", min_score=None, *, is_current=None):
        if kind not in KINDS or (genre and genre not in GENRES) or min_score not in (None, 8):
            return FilmBatch(message="请使用电影、电视剧或动漫，以及明确的类型", kind=kind), self.begin(is_current)
        current = self.begin(is_current)
        result = self.discovery.read(kind)
        if not current():
            return FilmBatch(message="已忽略过时的推荐", kind=kind), current
        rows = deepcopy(result.get("works") or [])
        if kind == "movie" and genre:
            # Reuse the existing public, credential-free tag-filtered metadata reader.
            try:
                for index, item in enumerate(self.metadata(genre, page_limit=20) or []):
                    score = item.get("rate")
                    try:
                        score = float(score)
                    except (ValueError, TypeError):
                        score = None
                    if score is not None and not 0 < score <= 10:
                        score = None
                    rows.append(dict(title=item.get("title", ""), kind="movie", genres=[genre],
                        tag_basis="豆瓣分类标签", score=score, score_source="豆瓣" if score else "",
                        cover_url=item.get("cover", ""), sources=[dict(platform="douban", platform_name="豆瓣",
                        source_id=item.get("id"), rank=index + 1, url=item.get("url"), board=genre + "分类")]))
            except Exception:
                pass  # Strict filters below never disguise missing metadata as a match.
        snapshot = self.memory.snapshot(kind)
        profile = snapshot["profile"]
        explicit, inferred = profile["explicit"], profile["inferred"]
        records, feedback = snapshot["records"], snapshot["feedback"]
        candidates, seen = [], set()
        for work in rows:
            if work.get("kind") != kind or not work.get("title"):
                continue
            key = work_key(work)
            related = matching_record(work, feedback)
            content_preference = (1 if related["positive"] else -1) if related else explicit.get(("content", key), 0)
            if key in seen or content_preference == -1:
                continue
            seen.add(key)
            if genre and genre not in work.get("genres", []):
                continue
            score = work.get("score")
            if min_score is not None and (type(score) not in (int, float) or not 0 < score <= 10
                                          or not work.get("score_source") or score < min_score):
                continue
            record = matching_record(work, records)
            if record and record["state"] == "continuing":
                continue  # A partial watch belongs in continue-watching, not new-film cards.
            features = tags(work)
            if any(explicit.get(tag) == -1 and tag != ("genre", genre) for tag in features):
                continue
            preference = content_preference + sum(explicit.get(tag, 0) for tag in features)
            history = sum(inferred.get(tag, 0) for tag in features)
            rank = min((s.get("rank") for s in work.get("sources", []) if type(s.get("rank")) is int
                        and s["rank"] > 0), default=100)
            work["recommendation_reason"] = ("符合本次类型要求" if genre else
                "参考你明确表达的喜好" if preference > 0 else "参考本地偏好记录" if history else "来自可用热门榜单")
            candidates.append(((not record or record["state"] != "completed", preference, history), rank, work))
        selected = []
        while candidates and len(selected) < 6:
            tier = max(row[0] for row in candidates)
            pool = [row for row in candidates if row[0] == tier]
            pool.sort(key=lambda row: row[1])
            pool = pool[:12]
            picked = self.rng.choices(pool, weights=[row[1] ** -1.5 for row in pool], k=1)[0]
            selected.append(picked[2])
            candidates.remove(picked)
        failures = any(s.get("state") in ("failed", "stale") for s in result.get("states", []))
        message = f"为你找到 {len(selected)} 部{CATEGORIES[kind]}；请选择后搜索片源"
        if not selected:
            message = ("当前公开数据不足以确认类型或评分，可重试或查看热门榜单" if genre or min_score else
                       "暂时没有可用推荐，请刷新榜单；本地影片和继续观看仍可使用")
        elif failures:
            message += "；部分来源暂不可用或使用缓存"
        if not self.memory.available:
            message += "；偏好暂不可读写，本次按榜单筛选"
        return FilmBatch(selected, message, kind, states=result.get("states", [])), current

    def continuing(self, kind="tv", *, is_current=None):
        current = self.begin(is_current)
        rows = self.resume_rows(kind)
        message = "正在恢复上次观看的位置" if len(rows) == 1 else "请选择要继续观看的内容" if rows else "还没有可靠的续播记录，请从播放历史选择"
        if not rows and callable(getattr(self.movie, "get_history", None)):
            history = self.movie.get_history()
            rows = [dict(title=r.get("title", "历史视频"), _history_row=r) for r in history]
            if rows:
                return FilmBatch(rows, "旧历史缺少可靠分类或进度，请选择要继续观看的记录", kind, "history"), current
        return FilmBatch(rows, message, kind, "resume"), current

    def resume_rows(self, kind):
        rows = []
        for row in self.memory.records(kind):
            if row["state"] != "continuing" or not row["item"].get("first_url"):
                continue
            item = dict(row["item"], _starlight_work=row["work"])
            if item.get("local_file"):
                from pathlib import Path
                if not Path(item["first_url"]).is_file():
                    continue
            eps = episode_eps(item)
            if eps and not 0 <= row["episode"] < len(eps):
                continue
            rows.append(dict(row["work"], resume_seconds=row["position"],
                             _resume_item=item, _resume_episode=row["episode"], updated=row["updated"]))
        return rows

    def feedback(self, work, positive):
        self.cancel()
        return self.memory.remember(work, positive)
