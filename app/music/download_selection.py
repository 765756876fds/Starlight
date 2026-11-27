"""Evidence and constraints for one download rendition."""
from dataclasses import dataclass

LOSSLESS_CODECS = frozenset(("flac", "alac", "ape", "wavpack", "pcm"))
TIER_ORDER = {name: i for i, name in enumerate(
    ("low", "standard", "high", "super", "lossless", "hires"), 1)}
CONTAINERS = {"mp3": ".mp3", "flac": ".flac", "wav": ".wav", "wave": ".wav",
              "ogg": ".ogg", "opus": ".ogg", "ape": ".ape", "mp4": ".m4a", "m4a": ".m4a"}


def codec_family(codec):
    codec = (codec or "").casefold()
    return "pcm" if codec.startswith("pcm") else codec or None


def quality_label(codec, bitrate=None, bits=None):
    codec = codec_family(codec)
    if codec in LOSSLESS_CODECS:
        return f"{bits} bit 无损" if bits else "无损"
    return f"{round(bitrate / 1000)} kbps" if bitrate else "音质待校验"


def media_score(codec, bitrate=None, bits=None, rate=None):
    codec = codec_family(codec)
    if codec in LOSSLESS_CODECS:
        return 4, bits or 0, rate or 0
    if not codec:
        return 0, 0, 0
    bitrate = bitrate or 0
    return (3 if bitrate >= 256_000 else 2 if bitrate >= 160_000 else 1,
            bitrate, rate or 0)


@dataclass(frozen=True)
class DownloadOption:
    owner: str
    track_id: str
    tier: str | None
    native: str | None
    extension: str | None
    codec: str | None
    size_bytes: int | None
    bitrate: int | None
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None
    size_evidence: str | None
    declared_quality: str | None

    @property
    def resource(self):
        return self.owner, self.track_id, self.native

    @property
    def score(self):
        return media_score(self.codec, self.bitrate, self.bit_depth, self.sample_rate)

    @property
    def label(self):
        return quality_label(self.codec, self.bitrate, self.bit_depth)

    def matches(self, info):
        return ((not self.extension or info["extension"] == self.extension)
                and (not self.codec or codec_family(info.get("codec")) == codec_family(self.codec))
                and (not self.bit_depth or info.get("bits_per_sample") == self.bit_depth)
                and (not self.sample_rate or info.get("sample_rate") == self.sample_rate)
                and (not self.channels or info.get("channels") == self.channels)
                and (not self.bitrate or info.get("bitrate", 0) >= self.bitrate
                     or codec_family(self.codec) in LOSSLESS_CODECS))


@dataclass
class DownloadOffer:
    track: object
    result: dict
    option: DownloadOption
    priority: int
    evidence: str
