"""Owner-configured routes from reviewed catalog metadata to LX resolvers."""
from dataclasses import dataclass, replace
import re

from ..download_journal import _raw, plain


PLATFORMS = frozenset({"kw", "kg", "tx", "wy", "mg", "bd"})
_NATIVE = re.compile(r"[A-Za-z0-9_-]{1,160}", re.ASCII)


@dataclass(frozen=True)
class CatalogRoute:
    catalog: str
    platform: str
    resolvers: tuple[str, ...]


def validate_routes(routes, sources):
    by_name = {source.name: source for source in sources}
    catalogs = set()
    for route in routes:
        catalog = by_name.get(route.catalog)
        if (catalog is None or catalog.plugin_type != "musicfree" or route.catalog in catalogs
                or route.platform not in PLATFORMS or not 1 <= len(route.resolvers) <= 3
                or len(set(route.resolvers)) != len(route.resolvers)):
            raise ValueError("invalid_catalog_route")
        for name in route.resolvers:
            resolver = by_name.get(name)
            if resolver is None or resolver.plugin_type != "lx" or route.platform not in resolver.providers:
                raise ValueError("invalid_catalog_resolver")
        catalogs.add(route.catalog)


def stored_route(value):
    if (not isinstance(value, dict) or set(value) != {"source", "plugin", "track", "platform"}
            or not plain(value["source"], 128) or not plain(value["plugin"], 128)
            or not plain(value["track"]) or type(value["platform"]) is not str or value["platform"] not in PLATFORMS):
        raise ValueError("invalid_catalog_provenance")
    return dict(value)


def _native_id(value):
    text = str(value) if type(value) in (int, str) else ""
    return text if _NATIVE.fullmatch(text) and (type(value) is str or 0 <= value <= 2**53 - 1) else None


def _scoped_id(catalog, platform, native, info):
    if platform == "kg":
        # Kugou's songmid does not distinguish the hash variants.
        digest = _native_id(info.get("hash"))
        return f"{catalog}:{platform}:{native}:{digest}" if digest else None
    return f"{catalog}:{platform}:{native}"


def resolver_track(track, platform, owner):
    """Project only normalised LX musicInfo fields, never a search plugin's URL."""
    raw = _raw(track.extra.get("_raw")) if isinstance(track.extra, dict) else {}
    # An upstream MusicFree item.id is an address in its own namespace.
    # Require an explicit platform-native songmid in the catalog contract.
    native = _native_id(raw.get("songmid"))
    if not native or any(raw.get(key, platform) != platform for key in ("source", "platform")):
        return None
    scoped_id = _scoped_id(track.source_name, platform, native, raw)
    if scoped_id is None:
        return None
    info = {**raw, "source": platform, "id": raw["songmid"], "name": track.title, "singer": track.artist,
            "albumName": track.album}
    provenance = {"source": track.source_name, "plugin": track.authorization_plugin,
                  "track": track.track_id, "platform": platform}
    return replace(track, source_name=owner, track_id=scoped_id, cover_url=None, authorization_plugin=owner,
                   extra={"_raw": {**raw, "musicInfo": info}, "_catalog": provenance})


def routed_native_id(track_id, music_info, platform, catalog):
    native = _native_id(music_info.get("songmid"))
    if (native and music_info.get("source") == platform and _native_id(music_info.get("id")) == native
            and track_id == _scoped_id(catalog, platform, native, music_info)):
        return native
    return None


def original_catalog_track(track, ref):
    raw = _raw(track.extra.get("_raw"))
    raw.pop("musicInfo", None)
    return replace(track, source_name=ref["source"], track_id=ref["track"],
                   authorization_plugin=ref["plugin"], extra={"_raw": raw})


def resolver_quality_fields(info):
    """Adapt whitelisted catalog declarations to LX's two quality views."""
    qualities = info.get("qualities", {})
    fields = {}
    for key, value in qualities.items():
        facts = {name: item for name, item in value.items() if name != "hash" or _native_id(item)}
        fields[key] = facts
    return {"_types": fields, "types": [{**facts, "type": key} for key, facts in fields.items()]}
