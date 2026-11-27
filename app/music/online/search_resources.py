"""Metadata supplied by search adapters. Search performs no media probes."""
from dataclasses import dataclass
from decimal import Decimal
import re

from .content_choices import raw_track

_QUALITY = {
    "low": "低档", "128k": "128K 档", "standard": "标准档", "high": "高品质档",
    "320k": "320K 档", "super": "超高档", "flac": "FLAC 档",
    "lossless": "无损档", "hires": "Hi-Res 档", "flac24bit": "24-bit FLAC 档",
}
_FORMATS = {"mp3", "aac", "m4a", "flac", "wav", "ogg", "opus", "alac", "ape", "wma"}
_BYTES = re.compile(r"(\d{1,15}(?:\.\d{1,8})?)\s*(B|KIB|MIB|GIB)?", re.I)
_BINARY_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3}


def _integer(value, low=1, high=(1 << 53) - 1):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value.strip()):
        value = int(value)
    return value if type(value) is int and low <= value <= high else None


def _size(value):
    if type(value) is int:
        return _integer(value)
    match = _BYTES.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    result = Decimal(match[1]) * _BINARY_UNITS[(match[2] or "B").upper()]
    return _integer(int(result)) if result == result.to_integral_value() else None


@dataclass(frozen=True)
class SearchResource:
    source: str
    size_bytes: int | None
    declared_quality: str | None
    format: str | None
    bitrate_bps: int | None
    sample_rate_hz: int | None
    bit_depth: int | None
    evidence: str = "source_declared"

    def parameters(self):
        return " · ".join(filter(None, (
            f"{self.bitrate_bps / 1000:g} kbps" if self.bitrate_bps else "",
            f"{self.sample_rate_hz / 1000:g} kHz" if self.sample_rate_hz else "",
            f"{self.bit_depth} bit" if self.bit_depth else "",
        )))

    def brief(self):
        if not self.size_bytes:
            size = ""
        elif self.size_bytes >= 1024**2:
            size = f"{self.size_bytes / 1024**2:.1f} MiB"
        elif self.size_bytes >= 1024:
            size = f"{self.size_bytes / 1024:.1f} KiB"
        else:
            size = f"{self.size_bytes} B"
        return " · ".join(filter(None, (size, _QUALITY.get(self.declared_quality), self.format)))


def _resource(track, fields, quality=None):
    raw_format = fields.get("format") or fields.get("ext") or fields.get("container")
    fmt = raw_format.strip().lower().removeprefix(".") if isinstance(raw_format, str) else ""
    fmt = fmt.upper() if fmt in _FORMATS else None
    size = _size(fields.get("sizeBytes", fields.get("size_bytes", fields.get("size"))))
    bitrate = _integer(fields.get("bitrate_bps"), 8000, 100_000_000)
    if bitrate is None:
        kbps = _integer(fields.get("bitrate_kbps"), 8, 100_000)
        bitrate = kbps * 1000 if kbps else None
    if bitrate is None:
        value = fields.get("bitrate")
        if isinstance(value, str):
            match = re.fullmatch(r"(\d{1,8})\s*(kbps|bps)", value.strip(), re.I)
            if match:
                bitrate = int(match[1]) * (1000 if match[2].casefold() == "kbps" else 1)
                bitrate = _integer(bitrate, 8000, 100_000_000)
    rate = _integer(fields.get("sampleRate", fields.get("sample_rate_hz", fields.get("sampleRateHz"))),
                    8000, 1_536_000)
    depth = _integer(fields.get("bitDepth", fields.get("bit_depth", fields.get("bits_per_sample"))), 8, 64)
    return SearchResource(track.source_name, size, quality, fmt, bitrate, rate, depth)


def search_resources(track):
    """Each MusicFree qualities entry describes a separate provider rendition."""
    raw = raw_track(track)
    qualities = raw.get("qualities")
    if isinstance(qualities, dict):
        options = tuple(_resource(track, qualities[key], key) for key in _QUALITY
                        if isinstance(qualities.get(key), dict))
        if options:
            return options
    hint = raw.get("quality")
    primary = _resource(track, raw, hint if isinstance(hint, str) and hint in _QUALITY else None)
    return (primary,) if any((primary.size_bytes, primary.declared_quality, primary.format,
                               primary.bitrate_bps, primary.sample_rate_hz, primary.bit_depth)) else ()


def source_play_count(track):
    """Explicit track-level provider count. No search-position or playlist proxy."""
    return _integer(raw_track(track).get("playCount"), low=0)
