"""Owner-selected launch configuration for the reviewed Windows music host."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..catalog_routes import CatalogRoute, PLATFORMS, validate_routes
from ..plugin_trust import MAX_DOCUMENT, TrustPolicy, _owned_directory, _plain_file
from ..source_authority import ResourcePolicy, TransferBudget


_ID = re.compile(r"[a-zA-Z0-9_.-]{1,80}")
_ROLES = {"host_http", "playback", "download", "cover", "lyrics", "metadata"}
MAX_ISOLATED_SOURCES = 4


@dataclass(frozen=True)
class IsolatedSourceSettings:
    name: str
    plugin_type: str
    providers: tuple[str, ...]
    policies: Mapping[str, ResourcePolicy]
    test_loopback_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class IsolatedMusicSettings:
    work_parent: Path
    trust_root: Path
    node: Path
    policy: TrustPolicy
    sources: tuple[IsolatedSourceSettings, ...]
    # Loopback authorizations are available only to isolated test harnesses
    # constructing these settings directly. The CLI does not parse them.
    idle_timeout: float = field(default=600.0, repr=False)
    catalog_routes: tuple[CatalogRoute, ...] = ()


def _absolute(value):
    if not isinstance(value, str):
        raise ValueError("isolated_host_path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("isolated_host_path")
    return path


def _document_bytes(path):
    original = _plain_file(_owned_directory(path.parent) / path.name)
    with path.open("rb") as stream:
        if not os.path.samestat(original, os.fstat(stream.fileno())):
            raise ValueError("isolated_host_document")
        value = stream.read(MAX_DOCUMENT + 1)
    if len(value) > MAX_DOCUMENT:
        raise ValueError("isolated_host_document")
    return value


def _unique(pairs):
    entries = dict(pairs)
    if len(entries) != len(pairs):
        raise ValueError("isolated_host_duplicate_key")
    return entries


def _list(value, *, allowed=None):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("isolated_host_list")
    if len(set(value)) != len(value) or (allowed is not None and set(value) - allowed):
        raise ValueError("isolated_host_list")
    return tuple(value)


def _policies(raw):
    if not isinstance(raw, dict) or not raw or raw.keys() - _ROLES:
        raise ValueError("isolated_host_policies")
    policies = {}
    for role, entry in raw.items():
        if (not isinstance(entry, dict) or not {"origins", "timeout", "body_bytes"} <= entry.keys()
                or entry.keys() - {"origins", "timeout", "body_bytes", "headers", "methods", "requests",
                                  "header_bytes", "upload_bytes"}):
            raise ValueError("isolated_host_policy")
        methods = _list(entry["methods"], allowed={"GET", "HEAD", "POST"}) if "methods" in entry else None
        budget = TransferBudget(entry["timeout"], entry["body_bytes"],
                                header_bytes=entry.get("header_bytes", 32768),
                                requests=entry.get("requests", 5),
                                upload_bytes=entry.get("upload_bytes", 16384))
        if role == "host_http" and budget.body_bytes > 64 * 1024:
            raise ValueError("isolated_host_http_budget")
        policies[role] = ResourcePolicy(
            _list(entry["origins"]), budget, frozenset(_list(entry.get("headers", []))), methods
        )
    return MappingProxyType(policies)


def load_isolated_music_settings(path):
    """Read an explicit, trusted local owner document without importing plugins."""
    document = json.loads(_document_bytes(_absolute(path)), object_pairs_hook=_unique)
    required = {"work_parent", "trust_root", "node", "trust_policy", "trust_policy_sha256", "sources"}
    if (not isinstance(document, dict) or required - document.keys()
            or document.keys() - required - {"catalog_routes"}):
        raise ValueError("isolated_host_settings")
    policy = TrustPolicy.from_owner_document(
        _document_bytes(_absolute(document["trust_policy"])), pinned_sha256=document["trust_policy_sha256"]
    )
    raw_sources = document["sources"]
    if not isinstance(raw_sources, list) or not 0 < len(raw_sources) <= MAX_ISOLATED_SOURCES:
        raise ValueError("isolated_host_sources")
    sources = []
    for item in raw_sources:
        if not isinstance(item, dict) or item.keys() != {"plugin", "type", "providers", "policies"}:
            raise ValueError("isolated_host_source")
        name = item["plugin"]
        if not isinstance(name, str) or not _ID.fullmatch(name) or name not in policy.plugins:
            raise ValueError("isolated_host_plugin")
        kind = item["type"]
        providers = _list(item["providers"], allowed=PLATFORMS)
        if kind not in ("musicfree", "lx") or bool(providers) != (kind == "lx"):
            raise ValueError("isolated_host_adapter")
        sources.append(IsolatedSourceSettings(name, kind, providers, _policies(item["policies"])))
    names = {source.name.casefold() for source in sources}
    digests = {policy.plugins[source.name].runtime_sha256 for source in sources}
    if len(names) != len(sources) or "js_bridge" in names or len(digests) != 1:
        raise ValueError("isolated_host_sources")
    raw_routes = document.get("catalog_routes", [])
    if not isinstance(raw_routes, list) or len(raw_routes) > MAX_ISOLATED_SOURCES:
        raise ValueError("invalid_catalog_route")
    routes = []
    for route in raw_routes:
        if (not isinstance(route, dict) or route.keys() != {"catalog", "platform", "resolvers"}
                or not isinstance(route["catalog"], str) or not isinstance(route["platform"], str)):
            raise ValueError("invalid_catalog_route")
        routes.append(CatalogRoute(route["catalog"], route["platform"], _list(route["resolvers"])))
    validate_routes(routes, sources)
    return IsolatedMusicSettings(
        _absolute(document["work_parent"]), _absolute(document["trust_root"]), _absolute(document["node"]),
        policy, tuple(sources), catalog_routes=tuple(routes)
    )
