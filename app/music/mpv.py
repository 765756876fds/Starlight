"""
mpv 控制器 - 通过 JSON IPC (Windows Named Pipe) 控制 mpv

mpv 作为纯音频后端运行，无 GUI，不抢焦点。
Python 通过命名管道发送 JSON 命令控制播放。
"""
import json
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional


class MpvController:
    """mpv JSON IPC 控制器"""

    def __init__(self, mpv_path: str, ipc_pipe: str = "\\\\.\\pipe\\lol_music_mpv"):
        self.mpv_path = mpv_path
        self.ipc_pipe = ipc_pipe
        self._process: Optional[subprocess.Popen] = None
        self._pipe = None
        self._running = False

        self._request_id = 0
        self._properties: Dict[str, Any] = {}
        self._property_watchers: Dict[str, Any] = {}
        self._pipe_lock = threading.Lock()

    def start(self):
        """启动 mpv 进程"""
        if self._running:
            return

        if not os.path.exists(self.mpv_path):
            raise FileNotFoundError(f"mpv 可执行文件不存在: {self.mpv_path}")

        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "mpv.exe"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
        except Exception:
            pass

        self.ipc_pipe = r"\\.\pipe\lol_music_mpv_" + str(os.getpid())

        print(f"[mpv] pipe name: {self.ipc_pipe!r}")
        args = [
            self.mpv_path,
            "--no-video",
            "--no-terminal",
            "--force-window=no",
            f"--input-ipc-server={self.ipc_pipe}",
            "--idle=yes",
            "--tls-verify=no",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        ]

        self._process = subprocess.Popen(
            args,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        self._wait_for_pipe(timeout=10.0)
        self._running = True
        self._init_properties()
        print(f"[mpv] 已启动, PID={self._process.pid}")

    def _wait_for_pipe(self, timeout: float = 10.0):
        start = time.time()
        pipe_exists = False
        while time.time() - start < timeout:
            try:
                if os.path.exists(self.ipc_pipe):
                    pipe_exists = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        if not pipe_exists:
            raise TimeoutError(f"mpv IPC 管道超时未出现: {self.ipc_pipe}")
        time.sleep(0.3)
        last_error = None
        for i in range(5):
            try:
                self._pipe = open(self.ipc_pipe, "r+", buffering=1, encoding="utf-8")
                return
            except Exception as e:
                last_error = e
                time.sleep(0.2)
        raise ConnectionError(f"无法连接到 mpv IPC 管道: {last_error}")

    def _init_properties(self):
        properties = ["pause", "volume", "idle-active", "filename", "playback-time", "duration"]
        for prop in properties:
            try:
                value = self.get_property(prop, timeout=1.0)
                if value is not None:
                    self._properties[prop] = value
            except Exception:
                pass

    def _send_command(self, command: str, params: Optional[List[Any]] = None,
                       timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        if not self._pipe:
            raise ConnectionError("mpv IPC 未连接")
        with self._pipe_lock:
            self._request_id += 1
            req_id = self._request_id
            message = {"command": [command] + (params or []), "request_id": req_id}
            line = json.dumps(message) + "\n"
            self._pipe.write(line)
            self._pipe.flush()
            start = time.time()
            resp = None
            while time.time() - start < timeout:
                line = self._pipe.readline()
                if not line:
                    time.sleep(0.01)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "event" in msg:
                    continue
                if msg.get("request_id") == req_id:
                    resp = msg
                    break
            return resp

    def loadfile(self, filepath: str, mode: str = "replace"):
        print(f"[mpv] cmd loadfile: {filepath[:80]} (mode={mode})")
        resp = self._send_command("loadfile", [filepath, mode], timeout=5.0)
        err = (resp or {}).get("error")
        print(f"[mpv] resp: error={err}")

    def play(self):
        self.set_property("pause", False)

    def pause(self):
        self.set_property("pause", True)

    def toggle_pause(self):
        self._send_command("cycle", ["pause"])

    def stop(self):
        self._send_command("stop")

    def seek(self, seconds: float, mode: str = "relative"):
        self._send_command("seek", [seconds, mode])

    def set_volume(self, volume: int):
        volume = max(0, min(100, volume))
        self.set_property("volume", float(volume))
        self._properties["volume"] = float(volume)

    def get_volume(self) -> int:
        vol = self.get_property("volume", timeout=1.0)
        if vol is not None:
            self._properties["volume"] = vol
            return int(vol)
        return int(self._properties.get("volume", 0))

    def set_property(self, name: str, value: Any):
        self._send_command("set_property", [name, value])

    def get_property(self, name: str, timeout: float = 2.0) -> Any:
        resp = self._send_command("get_property", [name], timeout=timeout)
        if resp and "data" in resp:
            return resp["data"]
        return None

    def get_current_time(self) -> float:
        return float(self._properties.get("playback-time", 0))

    def get_live_playback_time(self) -> float:
        try:
            val = self.get_property("playback-time", timeout=1.0)
            if val is not None:
                self._properties["playback-time"] = val
                return float(val)
        except Exception:
            pass
        return self.get_current_time()

    def get_duration(self) -> float:
        try:
            val = self.get_property("duration", timeout=1.0)
            if val is not None:
                self._properties["duration"] = val
                return float(val)
        except Exception:
            pass
        return float(self._properties.get("duration", 0))

    def is_paused(self) -> bool:
        return bool(self._properties.get("pause", False))

    def is_idle(self) -> bool:
        return bool(self._properties.get("idle-active", False))

    def get_filename(self) -> str:
        return str(self._properties.get("filename", ""))

    def on_property_change(self, prop: str, callback: Callable[[Any], None]):
        self._property_watchers[prop] = callback

    def is_running(self) -> bool:
        if not self._running or not self._process:
            return False
        return self._process.poll() is None

    def stop_process(self):
        self._running = False
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        print("[mpv] 已停止")

    def __del__(self):
        try:
            self.stop_process()
        except Exception:
            pass
