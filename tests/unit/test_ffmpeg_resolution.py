from __future__ import annotations

from pathlib import Path

from app.third_party.fingerprint.core import decoder


def test_bundled_ffmpeg_is_preferred(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "third_party" / "ffmpeg" / "ffmpeg.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"fake")
    monkeypatch.setattr(decoder, "__file__", str(tmp_path / "app" / "third_party" / "fingerprint" / "core" / "decoder.py"))
    monkeypatch.setattr(decoder.shutil, "which", lambda _: "C:\\PATH\\ffmpeg.exe")
    assert decoder._bundled_ffmpeg_path() == str(bundled)
    assert decoder._resolve_ffmpeg("ffmpeg") == str(bundled)


def test_ffmpeg_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(decoder, "_bundled_ffmpeg_path", lambda: None)
    monkeypatch.setattr(decoder.shutil, "which", lambda name: "C:\\PATH\\ffmpeg.exe" if name == "ffmpeg" else None)
    assert decoder._resolve_ffmpeg("ffmpeg") == "C:\\PATH\\ffmpeg.exe"


def test_missing_ffmpeg_returns_none(monkeypatch):
    monkeypatch.setattr(decoder, "_bundled_ffmpeg_path", lambda: None)
    monkeypatch.setattr(decoder.shutil, "which", lambda _: None)
    assert decoder._resolve_ffmpeg("ffmpeg") is None
