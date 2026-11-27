
"""Host-issued authorization for an opt-in remote music source.

The legacy host does not mint these objects. The source host owns the policy
and passes a lease to its reviewed adapter, never to plugin code or a journal.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_address
import math
import re
from threading import Lock
from types import MappingProxyType

from yarl import URL

_KINDS = frozenset({"playback", "download", "cover", "lyrics", "metadata", "host_http"})
_METHODS = {
    "playback": frozenset({"GET", "HEAD"}), "download": frozenset({"GET", "HEAD"}),
    "cover": frozenset({"GET"}), "lyrics": frozenset({"GET"}), "metadata": frozenset({"GET"}),
    "host_http": frozenset({"GET", "HEAD", "POST"}),
}
_HEADER_NAME = re.compile(r"[a-z0-9!#$%&'*+.^_~|-]{1,64}", re.ASCII)
_PUBLIC_HEADERS = frozenset({"accept", "user-agent"})
_FORBIDDEN = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "proxy-connection",
    "proxy-authorization", "proxy-authenticate", "upgrade", "te", "trailer", "keep-alive",
    "accept-encoding", "range", "if-range", "cookie", "cookie2", "authorization",
})
_IDENTITY = re.compile(r"[\w.:-]{1,128}", re.UNICODE)
_MAX_SOURCES = 64


class DownloadTransportError(ValueError):
    """A stable transport failure reason without a URL or request data."""

    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def authorization_plugin(track):
    """Extract the durable identity from a trusted model row, if present."""
    value = getattr(track, "authorization_plugin", None)
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def parse_remote_url(value):
    if not isinstance(value, str) or not value or len(value) > 16384 or any(
            ord(ch) <= 32 or ord(ch) == 127 or ch == "\\" for ch in value):
        raise DownloadTransportError("invalid_url")
    try:
        parsed = URL(value, encoded=True)
        authority = value.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host, port = parsed.raw_host, parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or not host.isascii()
                or "%" in host or "@" in authority or parsed.raw_user is not None
                or parsed.raw_password is not None or not 0 < port <= 65535):
            raise DownloadTransportError("blocked_target")
        return parsed.with_fragment(None)
    except DownloadTransportError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise DownloadTransportError("invalid_url") from None


def _canonical_origin(value):
    url = parse_remote_url(value)
    if url.raw_path not in ("", "/") or url.raw_query_string or "#" in value:
        raise ValueError("an exact origin is required")
    return url.origin()


@dataclass(frozen=True)
class TransferBudget:
    timeout: float
    body_bytes: int
    header_bytes: int = 32 * 1024
    requests: int = 5
    upload_bytes: int = 16 * 1024

    def __post_init__(self):
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 3600):
            raise ValueError("invalid timeout")
        for value, lower, upper in ((self.body_bytes, 1, 2**40), (self.header_bytes, 1, 65536),
                                    (self.requests, 1, 12), (self.upload_bytes, 0, 65536)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid transfer budget")


@dataclass(frozen=True)
class ResourcePolicy:
    origins: tuple[str, ...]
    budget: TransferBudget
    extra_headers: frozenset[str] = frozenset()
    methods: frozenset[str] | None = None

    def __post_init__(self):
        origins = frozenset(_canonical_origin(value) for value in self.origins)
        headers = frozenset(name.lower() for name in self.extra_headers)
        if (not 0 < len(origins) <= 32 or not isinstance(self.budget, TransferBudget)
                or len(headers) > 16 or headers & _FORBIDDEN
                or any(not _HEADER_NAME.fullmatch(name) for name in headers)):
            raise ValueError("invalid resource policy")
        object.__setattr__(self, "origins", origins)
        object.__setattr__(self, "extra_headers", headers)
        if self.methods is not None:
            object.__setattr__(self, "methods", frozenset(self.methods))


@dataclass(frozen=True, eq=False)
class SourceLease:
    source_id: str
    plugin_id: str
    version: int
    _authority: SourceAuthority = field(repr=False)
    _policies: object = field(repr=False)
    _test_origins: frozenset = field(repr=False)

    def active(self):
        return self._authority.active(self)

    def retire(self):
        self._authority.revoke(self)

    def resource(self, kind, url, headers=None, *, resource_id):
        self._authority.require_live(self)
        policy = self._policies.get(kind)
        if policy is None or not isinstance(resource_id, str) or not 0 < len(resource_id) <= 512:
            raise DownloadTransportError("source_scope")
        target = parse_remote_url(url)
        if target.origin() not in policy.origins:
            raise DownloadTransportError("source_origin")
        clean = {}
        if headers is not None and not isinstance(headers, dict):
            raise DownloadTransportError("source_headers")
        if headers and len(headers) > 16:
            raise DownloadTransportError("source_headers")
        for name, value in (headers or {}).items():
            key = name.lower() if isinstance(name, str) else ""
            if (key not in _PUBLIC_HEADERS | policy.extra_headers or key in clean
                    or not isinstance(value, str) or not 0 < len(value) <= 4096
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise DownloadTransportError("source_headers")
            clean[key] = value
        return AuthorizedResource(self, kind, resource_id, str(target), MappingProxyType(clean), policy)


@dataclass(frozen=True, eq=False)
class AuthorizedResource:
    lease: SourceLease = field(repr=False)
    kind: str
    resource_id: str = field(repr=False)
    url: str = field(repr=False)
    headers: object = field(repr=False)
    _policy: ResourcePolicy = field(repr=False)

    @property
    def budget(self):
        return self._policy.budget

    def active(self):
        return self.lease.active()

    def check(self, kind, url):
        target = self.target(url)
        if kind != self.kind or str(target) != self.url:
            raise DownloadTransportError("source_scope")
        return target

    def check_owner(self, source, resource_id):
        self.lease._authority.require_live(self.lease)
        if self.lease.source_id != source or self.resource_id != resource_id:
            raise DownloadTransportError("source_scope")

    def target(self, url):
        self.lease._authority.require_live(self.lease)
        target = parse_remote_url(url)
        if target.origin() not in self._policy.origins:
            raise DownloadTransportError("source_origin")
        return target

    def local_allowed(self, url):
        return url.origin() in self.lease._test_origins

    def method_allowed(self, method):
        methods = self._policy.methods
        if methods is None:
            methods = _METHODS[self.kind] - {"POST"}
        return method in methods

    def require_active(self):
        self.lease._authority.require_live(self.lease)

    def resume_key(self, url):
        # Same owner, policy revision, track and effective representation address.
        # Instance replacement remains resumable only after current re-authorization.
        scope = (self.lease.source_id, self.lease.plugin_id, str(self.lease.version),
                 self.resource_id, str(url))
        encoded = "".join(f"{len(field)}:{field}" for field in scope)
        return sha256(encoded.encode("utf-8")).hexdigest()

    def slot(self):
        return self.lease._authority.slot(self.lease)


class SourceAuthority:
    """A trusted host's live registry. Only in-memory capabilities cross adapters."""

    def __init__(self, *, total_requests=6, requests_per_source=2):
        if not 1 <= requests_per_source <= total_requests <= 64:
            raise ValueError("invalid request slots")
        self._lock = Lock()
        self._live = {}
        self._floors = {}
        self._revoked = {}
        self._running = {}
        self._total_limit = total_requests
        self._source_limit = requests_per_source

    def bind(self, source_id, plugin_id, version, policies, *, test_loopback_origins=()):
        if (not isinstance(source_id, str) or not _IDENTITY.fullmatch(source_id)
                or not isinstance(plugin_id, str) or not _IDENTITY.fullmatch(plugin_id)
                or type(version) is not int or not 1 <= version <= 2**53
                or not policies or set(policies) - _KINDS
                or any(not isinstance(policy, ResourcePolicy) for policy in policies.values())):
            raise ValueError("invalid source authorization")
        if any(policy.methods is not None and (not policy.methods or policy.methods - _METHODS[role])
               for role, policy in policies.items()):
            raise ValueError("invalid request methods for resource kind")
        tests = frozenset(_canonical_origin(value) for value in test_loopback_origins)
        advertised = frozenset(origin for p in policies.values() for origin in p.origins)
        if tests - advertised or any(not ip_address(url.raw_host).is_loopback for url in tests):
            raise ValueError("test grants require an explicit loopback IP origin")
        lease = SourceLease(source_id, plugin_id, version, self, MappingProxyType(dict(policies)), tests)
        with self._lock:
            previous = self._floors.get(source_id)
            if (version <= self._revoked.get(source_id, 0)
                    or previous is None and len(self._floors) >= _MAX_SOURCES
                    or previous is not None and (version < previous[0]
                        or version == previous[0] and (dict(policies), tests, plugin_id) != previous[1])):
                raise DownloadTransportError("source_policy_version")
            self._floors[source_id] = (version, (dict(policies), tests, plugin_id))
            self._live[source_id] = lease
        return lease

    def revoke(self, lease):
        with self._lock:
            if self._live.get(lease.source_id) is lease:
                del self._live[lease.source_id]

    def revoke_policy(self, lease):
        with self._lock:
            if self._live.get(lease.source_id) is not lease:
                raise DownloadTransportError("source_inactive")
            self._revoked[lease.source_id] = lease.version
            del self._live[lease.source_id]

    def active(self, lease):
        with self._lock:
            return self._live.get(lease.source_id) is lease

    def require_live(self, lease):
        if not self.active(lease):
            raise DownloadTransportError("source_inactive")

    @contextmanager
    def slot(self, lease):
        with self._lock:
            if self._live.get(lease.source_id) is not lease:
                raise DownloadTransportError("source_inactive")
            count = self._running.get(lease.source_id, 0)
            if count >= self._source_limit or sum(self._running.values()) >= self._total_limit:
                raise DownloadTransportError("source_busy")
            self._running[lease.source_id] = count + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._running[lease.source_id] - 1
                if remaining:
                    self._running[lease.source_id] = remaining
                else:
                    del self._running[lease.source_id]
