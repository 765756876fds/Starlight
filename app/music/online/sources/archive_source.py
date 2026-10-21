"""Archive.org 公开音频源（合法、无需登录）。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..models import OnlineTrack
from ..source import MusicSource


class ArchiveSource(MusicSource):
    name = "archive"
    display_name = "Archive.org 公开音频"

    SEARCH_URL = "https://archive.org/advancedsearch.php"
    META_URL = "https://archive.org/metadata/{identifier}"
    DOWNLOAD_URL = "https://archive.org/download/{identifier}/{file}"
    TIMEOUT = 8.0

    def _http_get_json(self, url: str) -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lol-music-agent/1.0"})
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def search(self, keyword: str, limit: int = 20) -> list[OnlineTrack]:
        if not keyword:
            return []
        q = urllib.parse.quote(f"title:({keyword}) AND mediatype:audio")
        url = (
            f"{self.SEARCH_URL}?q={q}"
            f"&fl[]=identifier&fl[]=title&fl[]=creator"
            f"&rows={int(limit)}&output=json"
        )
        data = self._http_get_json(url)
        if not isinstance(data, dict):
            return []
        out: list[OnlineTrack] = []
        for doc in (data.get("response") or {}).get("docs") or []:
            ident = doc.get("identifier")
            if not ident:
                continue
            title = doc.get("title")
            if isinstance(title, list):
                title = title[0] if title else ""
            creator = doc.get("creator")
            if isinstance(creator, list):
                creator = creator[0] if creator else ""
            out.append(OnlineTrack(
                source_name=self.name,
                track_id=str(ident),
                title=str(title or ident),
                artist=str(creator or "Unknown"),
                album="",
                duration_seconds=0,
                extra={"identifier": ident},
            ))
        return out

    def get_play_url(self, track: OnlineTrack) -> str | None:
        ident = track.extra.get("identifier") or track.track_id
        meta = self._http_get_json(self.META_URL.format(identifier=ident))
        if not isinstance(meta, dict):
            return None
        for f in meta.get("files") or []:
            fmt = (f.get("format") or "").lower()
            name = f.get("name") or ""
            if "mp3" in fmt and name:
                return self.DOWNLOAD_URL.format(identifier=ident, file=urllib.parse.quote(name))
        return None

    def get_lyric(self, track: OnlineTrack) -> str | None:
        return None
