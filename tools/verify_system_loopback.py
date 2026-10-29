r"""Windows 系统声音一次性回环验收工具。

运行：.venv\Scripts\python.exe tools\verify_system_loopback.py
先播放本地音乐，再按 Enter 开始；录音只写临时 WAV，完成后删除。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import wave

# 允许从项目根目录直接运行，不要求用户设置 PYTHONPATH。
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.music.capture import CaptureCancelled, CaptureError, SystemLoopbackCapture


def _valid_wav(path: str) -> tuple[bool, str]:
    try:
        with wave.open(path, "rb") as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            raw = wav.readframes(frames)
    except (OSError, wave.Error) as exc:
        return False, f"WAV 文件无法读取：{exc}"
    if channels != 2 or width != 2 or rate != 48_000:
        return False, f"格式为 {rate} Hz、{channels} 声道、{width * 8} bit，不符合回环预期"
    if frames <= 0 or not raw or not any(raw):
        return False, "录音数据为空或全程静音"
    return True, f"WAV 有效：{rate} Hz、{channels} 声道、16 bit、{frames} 帧"


def main() -> int:
    input("请先播放本地音乐，准备好后按 Enter 开始采集约 8 秒：")
    fd, path = tempfile.mkstemp(prefix="starlight-loopback-", suffix=".wav")
    os.close(fd)
    try:
        print("正在采集 Windows 系统播放声音……")
        result = SystemLoopbackCapture(seconds=8).capture(path)
        valid, message = _valid_wav(result["path"])
        print(f"结果：{'成功' if valid else '失败'}。{message}")
        return 0 if valid else 1
    except CaptureCancelled:
        print("结果：已取消，系统声音资源已释放。")
        return 2
    except CaptureError as exc:
        print(f"结果：采集失败：{exc}")
        return 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
