"""Optional ShazamIO adapter for one-shot online song recognition.

This module is deliberately not imported by the normal local-identification
path.  ShazamIO is an unofficial client for a reverse-engineered service and
must remain an opt-in capability until it has passed real-device validation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import wave
from dataclasses import dataclass
from enum import Enum
from typing import Any


MIN_SECONDS = 8.0
MAX_SECONDS = 12.0
logger = logging.getLogger(__name__)


class ShazamRecognitionStatus(str, Enum):
    SUCCESS = "success"
    NO_MATCH = "no_match"
    DEPENDENCY_MISSING = "dependency_missing"
    INVALID_AUDIO = "invalid_audio"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    SERVICE_ERROR = "service_error"


@dataclass(frozen=True)
class ShazamRecognitionResult:
    status: ShazamRecognitionStatus
    title: str = ""
    artist: str = ""
    track_id: str = ""
    url: str = ""
    detail: str = ""


class ShazamIOProvider:
    """One-shot online recognizer; no audio is retained by this adapter."""

    def __init__(self, timeout_seconds: float = 20.0):
        self.timeout_seconds = max(0.01, float(timeout_seconds))

    @staticmethod
    def _validate_wav(path: str) -> str | None:
        if not os.path.isfile(path):
            return "音频文件不存在"
        try:
            with wave.open(path, "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                channels = wav.getnchannels()
                width = wav.getsampwidth()
        except (OSError, wave.Error) as exc:
            return f"WAV 文件无效：{exc}"
        if rate <= 0 or frames <= 0 or channels <= 0 or width <= 0:
            return "WAV 音频参数无效"
        duration = frames / rate
        if duration < MIN_SECONDS or duration > MAX_SECONDS:
            return f"音频时长需在 {MIN_SECONDS:g}-{MAX_SECONDS:g} 秒之间，当前 {duration:.1f} 秒"
        return None

    async def recognize_async(self, wav_path: str) -> ShazamRecognitionResult:
        error = self._validate_wav(wav_path)
        if error:
            return ShazamRecognitionResult(ShazamRecognitionStatus.INVALID_AUDIO, detail=error)
        try:
            from shazamio import Shazam, Serialize
        except ImportError as exc:
            logger.error("online_identify shazam_import_failed type=%s error=%s",
                         type(exc).__name__, str(exc)[:300])
            return ShazamRecognitionResult(
                ShazamRecognitionStatus.DEPENDENCY_MISSING,
                detail=f"在线识曲依赖加载失败：{exc}",
            )
        logger.info("online_identify shazam_import_ok")

        try:
            logger.info("online_identify shazam_request_started")
            async with Shazam() as client:
                payload = await asyncio.wait_for(
                    client.recognize(wav_path), timeout=self.timeout_seconds)
            logger.info("online_identify shazam_request_finished")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("online_identify result=timeout")
            return ShazamRecognitionResult(ShazamRecognitionStatus.TIMEOUT, detail="在线识曲请求超时")
        except Exception as exc:  # library exposes several aiohttp exception types
            status = getattr(exc, "status", None)
            logger.warning("online_identify request_failed type=%s status=%s error=%s",
                           type(exc).__name__, status, str(exc)[:300])
            if status == 429:
                return ShazamRecognitionResult(
                    ShazamRecognitionStatus.RATE_LIMITED, detail="在线识曲服务限流")
            if isinstance(exc, OSError) or exc.__class__.__module__.startswith("aiohttp"):
                return ShazamRecognitionResult(
                    ShazamRecognitionStatus.NETWORK_ERROR, detail=f"在线识曲网络错误：{exc}")
            return ShazamRecognitionResult(
                ShazamRecognitionStatus.SERVICE_ERROR, detail=f"在线识曲服务错误：{exc}")

        track: Any = payload.get("track") if isinstance(payload, dict) else None
        if not isinstance(track, dict):
            logger.info("online_identify result=no_match")
            return ShazamRecognitionResult(ShazamRecognitionStatus.NO_MATCH, detail="未找到匹配歌曲")
        title = str(track.get("title") or "").strip()
        artist = str(track.get("subtitle") or track.get("artist") or "").strip()
        if not title or not artist:
            logger.info("online_identify result=no_match_missing_fields")
            return ShazamRecognitionResult(ShazamRecognitionStatus.NO_MATCH, detail="返回结果缺少歌曲或歌手")
        logger.info("online_identify result=success track_id_present=%s", bool(track.get("key")))
        return ShazamRecognitionResult(
            ShazamRecognitionStatus.SUCCESS,
            title=title,
            artist=artist,
            track_id=str(track.get("key") or ""),
            url=str(track.get("url") or ""),
        )

    def recognize(self, wav_path: str) -> ShazamRecognitionResult:
        """Synchronous convenience wrapper for a worker thread."""
        try:
            return asyncio.run(self.recognize_async(wav_path))
        except asyncio.CancelledError:
            return ShazamRecognitionResult(ShazamRecognitionStatus.CANCELLED, detail="用户取消在线识曲")
