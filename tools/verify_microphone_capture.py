r"""Windows 本地麦克风一次性验收工具。

运行：.venv\Scripts\python.exe tools\verify_microphone_capture.py
只采集临时 WAV，不播放、不上传、不调用识曲服务；完成后删除临时文件。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import wave

from app.music.capture import CaptureCancelled, CaptureError, MicrophoneCapture


def _valid_wav(path: str) -> tuple[bool, str]:
    try:
        with wave.open(path, "rb") as wav:
            params = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes())
            raw = wav.readframes(wav.getnframes())
    except (OSError, wave.Error) as exc:
        return False, f"WAV 文件无法读取：{exc}"
    channels, width, rate, frames = params
    if (channels, width, rate) != (1, 2, 16_000):
        return False, f"格式不符合预期：{channels} 声道、{width * 8} bit、{rate} Hz"
    if frames <= 0 or not raw or not any(raw):
        return False, "录音数据为空或全程静音"
    return True, f"WAV 有效：{rate} Hz、{channels} 声道、16 bit、{frames} 帧"


def main() -> int:
    fd, path = tempfile.mkstemp(prefix="starlight-mic-check-", suffix=".wav")
    os.close(fd)
    cancel = threading.Event()
    finished = threading.Event()
    outcome: dict[str, object] = {}

    def record() -> None:
        try:
            outcome["result"] = MicrophoneCapture(seconds=8).capture(path, cancel)
        except CaptureCancelled as exc:
            outcome["error"] = exc
        except CaptureError as exc:
            outcome["error"] = exc
        finally:
            finished.set()

    def wait_for_cancel() -> None:
        try:
            input("录音中按 Enter 可取消（也可按 Ctrl+C）：")
            if not finished.is_set():
                cancel.set()
        except (EOFError, KeyboardInterrupt):
            cancel.set()

    print("星芒麦克风一次性验收")
    print("即将录音约 8 秒。请播放一段本地音乐或在麦克风前说话。")
    print("录音只写入临时文件，不会播放、上传或进行在线识曲。")
    input("准备好后按 Enter 开始；输入过程不会启动录音：")
    print("正在录音……请保持音源稳定。")
    threading.Thread(target=wait_for_cancel, daemon=True).start()
    worker = threading.Thread(target=record, daemon=True)
    worker.start()
    try:
        while not finished.wait(0.1):
            time.sleep(0.05)
    except KeyboardInterrupt:
        cancel.set()
        worker.join(timeout=2)
        print("已请求取消录音。")

    try:
        error = outcome.get("error")
        if isinstance(error, CaptureCancelled):
            print("结果：已取消，麦克风资源已释放。")
            return 2
        if isinstance(error, CaptureError):
            print(f"结果：录音失败：{error}")
            return 1
        valid, message = _valid_wav(path)
        print(f"结果：{'成功' if valid else '失败'}。{message}")
        return 0 if valid else 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
