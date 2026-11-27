"""Independent cursors and result retention for one manual music search."""
from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from .models import OnlineTrack, SearchPage, SearchResults
from .source import MusicSource, SourceSearchError


@dataclass(frozen=True)
class SearchTarget:
    owner: str
    name: str
    source: MusicSource
    version: int
    pageable: bool
    process: object = None

    @property
    def key(self):
        return self.owner, self.name


@dataclass
class _Cursor:
    target: SearchTarget
    next_page: int = 1
    ended: bool = False
    total: int | None = None
    tracks: list[OnlineTrack] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    error: str | None = None
    successful: bool = False
    deadline: float | None = None
    future: Future | None = None


class PagedSearch:
    """GUI-owned session. Only manager futures perform I/O."""

    WAIT_SECONDS = 10.0

    def __init__(self, manager, keyword, targets):
        self.manager = manager
        self.keyword = keyword
        self._cursors = [_Cursor(target) for target in targets]

    def _selected(self, owner):
        return [cursor for cursor in self._cursors if owner is None or cursor.target.owner == owner]

    def loading(self, owner=None):
        return any(cursor.deadline is not None for cursor in self._selected(owner))

    def can_fetch(self, owner=None):
        return any(not cursor.ended and cursor.error is None for cursor in self._selected(owner))

    def can_retry(self, owner=None):
        return any(not cursor.ended and cursor.error is not None for cursor in self._selected(owner))

    def request(self, owner=None, *, retry=False):
        now = time.monotonic()
        for cursor in self._selected(owner):
            if cursor.ended or cursor.deadline is not None or bool(cursor.error) != retry:
                continue
            cursor.deadline = now + self.WAIT_SECONDS
            cursor.error = None

    def poll(self):
        """Advance ready cursors without waiting for a single source."""
        changed = False
        now = time.monotonic()
        for cursor in self._cursors:
            if not self.manager._target_is_current(cursor.target):
                if cursor.error != "interrupted":
                    cursor.tracks.clear()
                    cursor.ids.clear()
                    cursor.successful = False
                    cursor.ended = True
                    cursor.error = "interrupted"
                    cursor.deadline = cursor.future = None
                    changed = True
                continue
            if cursor.deadline is None:
                continue
            if cursor.future is None and now < cursor.deadline:
                cursor.future = self.manager.submit_search_page(
                    cursor.target, self.keyword, cursor.next_page)
            if cursor.future is not None and cursor.future.done():
                self._collect(cursor)
            elif now >= cursor.deadline:
                cursor.error = "timeout" if cursor.future is not None else "busy"
            else:
                continue
            cursor.future = cursor.deadline = None
            changed = True
        return changed

    def _collect(self, cursor):
        try:
            page = cursor.future.result()
            if not cursor.target.pageable and isinstance(page, list):
                page = SearchPage(page, is_end=True)
            if not isinstance(page, SearchPage):
                raise SourceSearchError("structure")
        except Exception as exc:
            cursor.error = getattr(exc, "category", "timeout" if isinstance(exc, TimeoutError) else "unknown")
            return

        cursor.successful = True
        cursor.next_page += 1
        if page.total is not None:
            cursor.total = page.total
        for track in page.tracks:
            if track.track_id:
                if track.track_id in cursor.ids:
                    continue
                cursor.ids.add(track.track_id)
            cursor.tracks.append(track)
        cursor.ended = (not cursor.target.pageable or page.is_end is True
                        or (page.is_end is None and cursor.total is not None
                            and len(cursor.tracks) >= cursor.total))

    def results(self):
        out = SearchResults()
        for cursor in self._cursors:
            owner = cursor.target.owner
            out.setdefault(owner, []).extend(cursor.tracks)
            if cursor.successful:
                out.successful_sources.add(owner)
            if cursor.error:
                out.errors[owner] = cursor.error
        return out

    def errors(self, owner=None):
        return {cursor.target.key: cursor.error for cursor in self._selected(owner) if cursor.error}
