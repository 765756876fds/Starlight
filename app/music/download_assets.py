"""Validate downloaded media and attach only metadata belonging to that track."""
import logging
import os
import re
from pathlib import Path

from .lyrics import parse_lrc
from .online.popular_charts import normalized

log = logging.getLogger(__name__)


def inspect_audio(path, cancelled=None, *, stream=None):
    from mutagen import File
    media = File(path) if stream is None else File(fileobj=stream, filename=str(path))
    if media is None or not getattr(media.info, "length", 0) > 0:
        raise ValueError("not playable audio")
    kind = type(media).__name__
    extension = {"FLAC":".flac", "MP3":".mp3", "WAVE":".wav", "MP4":".m4a",
                 "OggVorbis":".ogg", "OggOpus":".ogg", "MonkeysAudio":".ape"}.get(kind)
    if not extension:
        raise ValueError("unsupported audio container")
    # Container headers alone can describe a complete song even when the body
    # is truncated. Decode supported formats in bounded blocks, off the UI thread.
    decoded = kind in ("FLAC", "MP3", "WAVE", "OggVorbis", "OggOpus")
    if decoded:
        import soundfile as sf
        if stream is not None:
            stream.seek(0)
        with sf.SoundFile(path if stream is None else stream) as decoder:
            frames = 0
            while True:
                if cancelled and cancelled():
                    raise InterruptedError("audio validation cancelled")
                block = decoder.read(8192, dtype="int16", always_2d=True)
                if not len(block):
                    break
                frames += len(block)
            decoded_duration = frames / decoder.samplerate
            pcm_lossless = decoder.subtype.startswith(("PCM_", "FLOAT", "DOUBLE"))
        if frames == 0 or abs(decoded_duration - media.info.length) > max(0.1, media.info.length * 0.01):
            raise ValueError("incomplete decoded audio")
    else:
        pcm_lossless = False
    rate = int(getattr(media.info, "sample_rate", 0) or 0)
    bitrate = int(getattr(media.info, "bitrate", 0) or 0)
    bits = int(getattr(media.info, "bits_per_sample", 0) or 0)
    lossless = kind in ("FLAC", "MonkeysAudio") or (kind == "WAVE" and pcm_lossless) or (
        kind == "MP4" and getattr(media.info, "codec", "") == "alac")
    grade = 4 if lossless else 3 if bitrate >= 256000 else 2 if bitrate >= 160000 else 1
    codec = {"FLAC": "flac", "MP3": "mp3", "WAVE": "pcm" if pcm_lossless else "wav-lossy",
             "OggVorbis": "vorbis", "OggOpus": "opus", "MonkeysAudio": "ape"}.get(kind)
    if kind == "MP4":
        tag = getattr(media.info, "codec", "") or ""
        codec = "aac" if tag.startswith("mp4a.40.") else tag or None
    return dict(extension=extension, duration=media.info.length, bitrate=bitrate,
                sample_rate=rate, bits_per_sample=bits, codec=codec,
                channels=getattr(media.info, "channels", None), lossless=lossless,
                evidence="decoded" if decoded else "metadata_parsed",
                score=(grade, bits if lossless else bitrate, rate),
                quality="无损" if lossless else f"{round(bitrate/1000)} kbps" if bitrate else "音质未标注")


def publish_new(part, final):
    """Same-directory publication; an existing file always wins."""
    if os.name == "nt":
        # Windows rename is atomic and refuses an existing destination, including
        # on removable filesystems which do not support hard links.
        os.rename(part, final)
    else:
        os.link(part, final)
        os.unlink(part)


def lyric_for_track(manager, track):
    text = manager.get_lyric(track)
    if not isinstance(text, str) or len(text.encode("utf-8")) > 2_000_000 or not parse_lrc(text):
        return None
    for key, expected in (("ti", track.title), ("ar", track.artist), ("al", track.album)):
        if not expected:
            continue
        match = re.search(r"\[" + key + r":([^\]]+)\]", text, re.I)
        if match and normalized(match[1]) != normalized(expected):
            return None
    return text


def cover_for_track(track, cancelled=None):
    from .online.download_probe import DownloadTransportError, read_download_attachment

    url = getattr(track, "cover_url", None)
    if not isinstance(url, str):
        return None
    try:
        data = read_download_attachment(url, cancelled=cancelled)
    except DownloadTransportError:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if 0 < width <= 4096 and 0 < height <= 4096 and width*height <= 4_000_000:
            return data, "image/png", ".png"
    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        return data, "image/jpeg", ".jpg"
    return None


def embed_tags(path, track, lyric=None, cover=None, *, stream=None):
    from mutagen import File
    from mutagen.flac import Picture
    from mutagen.id3 import TIT2, TPE1, TALB, USLT, APIC
    from mutagen.mp4 import MP4Cover
    media = File(path) if stream is None else File(fileobj=stream, filename=str(path))
    if media.tags is None:
        media.add_tags()
    if type(media).__name__ in ("MP3", "WAVE"):
        media.tags.add(TIT2(encoding=3, text=track.title))
        media.tags.add(TPE1(encoding=3, text=track.artist))
        media.tags.add(TALB(encoding=3, text=track.album))
        if lyric: media.tags.add(USLT(encoding=3, lang="zho", desc="", text=lyric))
        if cover: media.tags.add(APIC(encoding=3, mime=cover[1], type=3, desc="cover", data=cover[0]))
    elif type(media).__name__ == "MP4":
        media.tags["\xa9nam"], media.tags["\xa9ART"], media.tags["\xa9alb"] = [track.title], [track.artist], [track.album]
        if lyric: media.tags["\xa9lyr"] = [lyric]
        if cover: media.tags["covr"] = [MP4Cover(cover[0], imageformat=MP4Cover.FORMAT_PNG if cover[2]==".png" else MP4Cover.FORMAT_JPEG)]
    else:
        media.tags["title"], media.tags["artist"], media.tags["album"] = track.title, track.artist, track.album
        if lyric: media.tags["lyrics"] = lyric
        if cover and hasattr(media, "add_picture"):
            picture=Picture();picture.type=3;picture.mime=cover[1];picture.data=cover[0];media.add_picture(picture)
    if stream is None:
        media.save()
    else:
        stream.seek(0)
        media.save(fileobj=stream, filename=str(path))
        stream.flush()
        os.fsync(stream.fileno())


def save_sidecar(path, content):
    try:
        with Path(path).open("xb") as stream:
            stream.write(content)
        return "saved"
    except FileExistsError:
        return "preserved"
