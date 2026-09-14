"""
mpv 控制器 - 通过 JSON IPC (Windows Named Pipe) 控制 mpv

mpv 作为纯音频后端运行，无 GUI，不抢焦点。
Python 通过命名管道发送 JSON 命令控制播放。

架构：单连接同步读写
- 所有命令和响应都通过同一个连接
- 不使用后台读线程（避免 Python 文件对象线程安全问题）
- 每次 send_command 后同步读取响应
"""
import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional


class MpvController:
    """
    mpv JSON IPC 控制器

    使用 Windows Named Pipe 通信。
    mpv 以 --no-video --no-terminal 模式运行。
    """

    def __init__(self, mpv_path: str, ipc_pipe: str = "\\\\.\\pipe\\lol_music_mpv"):
        self.mpv_path = mpv_path
        self.ipc_pipe = ipc_pipe
        self._process: Optional[subprocess.Popen] = None
        self._pipe = None
        self._running = False

        self._request_id = 0
        self._properties: Dict[str, Any] = {}
        self._property_watchers: Dict[str, Any] = {}

    def start(self):
        """启动 mpv 进程"""
        if self._running:
            return

        # 确保 mpv 可执行文件存在
        if not os.path.exists(self.mpv_path):
            raise FileNotFoundError(f"mpv 可执行文件不存在: {self.mpv_path}")

        # 启动 mpv
        args = [
            self.mpv_path,
            "--no-video",
            "--no-terminal",
            "--force-window=no",
            f"--input-ipc-server={self.ipc_pipe}",
            "--idle=yes",
        ]

        self._process = subprocess.Popen(
            args,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        # 等待管道就绪并连接
        self._wait_for_pipe(timeout=10.0)
        self._running = True

        # 初始化常用属性
        self._init_properties()

        print(f"[mpv] 已启动, PID={self._process.pid}")

    def _wait_for_pipe(self, timeout: float = 10.0):
        """
        等待命名管道就绪并连接

        使用轮询方式，有明确超时。
        """
        start = time.time()

        # 先等待管道文件出现
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

        # 等待额外时间确保管道完全就绪
        time.sleep(0.3)

        # 尝试连接（带重试）
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
        """初始化常用属性"""
        properties = [
            "pause", "volume", "idle-active", "filename",
            "playback-time", "duration",
        ]
        for prop in properties:
            try:
                value = self.get_property(prop, timeout=1.0)
                if value is not None:
                    self._properties[prop] = value
            except Exception:
                pass

    def _send_command(self, command: str, params: Optional[List[Any]] = None,
                       timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        """
        发送命令到 mpv，并同步读取响应

        Args:
            command: 命令名
            params: 参数列表
            timeout: 等待响应超时

        Returns:
            响应消息
        """
        if not self._pipe:
            raise ConnectionError("mpv IPC 未连接")

        self._request_id += 1
        req_id = self._request_id

        message = {
            "command": [command] + (params or []),
            "request_id": req_id,
        }

        line = json.dumps(message) + "\n"
        self._pipe.write(line)
        self._pipe.flush()

        # 等待响应（跳过事件消息）
        start = time.time()
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

            # 事件消息，跳过
            if "event" in msg:
                # 处理属性变化事件
                if msg["event"] == "property-change":
                    prop = msg.get("name")
                    value = msg.get("data")
                    if prop:
                        self._properties[prop] = value
                        if prop in self._property_watchers:
                            try:
                                self._property_watchers[prop](value)
                            except Exception:
                                pass
                continue

            # 响应消息
            if msg.get("request_id") == req_id:
                return msg

        raise TimeoutError(f"mpv 命令超时: {command}")

    def loadfile(self, filepath: str, mode: str = "replace"):
        """加载并播放文件"""
        self._send_command("loadfile", [filepath, mode], timeout=5.0)

    def play(self):
        """播放（取消暂停）"""
        self.set_property("pause", False)

    def pause(self):
        """暂停"""
        self.set_property("pause", True)

    def toggle_pause(self):
        """切换暂停/播放"""
        self._send_command("cycle", ["pause"])

    def stop(self):
        """停止播放"""
        self._send_command("stop")

    def next(self):
        """下一首（播放列表）"""
        self._send_command("playlist_next")

    def prev(self):
        """上一首（播放列表）"""
        self._send_command("playlist_prev")

    def seek(self, seconds: float, mode: str = "relative"):
        """跳转"""
        self._send_command("seek", [seconds, mode])

    def set_volume(self, volume: int):
        """设置音量 0-100"""
        volume = max(0, min(100, volume))
        self.set_property("volume", float(volume))
        self._properties["volume"] = float(volume)

    def get_volume(self) -> int:
        """获取音量（实时从 mpv 读取）"""
        vol = self.get_property("volume", timeout=1.0)
        if vol is not None:
            self._properties["volume"] = vol
            return int(vol)
        return int(self._properties.get("volume", 0))

    def set_property(self, name: str, value: Any):
        """设置属性"""
        self._send_command("set_property", [name, value])

    def get_property(self, name: str, timeout: float = 2.0) -> Any:
        """获取属性"""
        resp = self._send_command("get_property", [name], timeout=timeout)
        if resp and "data" in resp:
            return resp["data"]
        return None

    def get_current_time(self) -> float:
        """获取当前播放位置（秒）"""
        return float(self._properties.get("playback-time", 0))

    def get_duration(self) -> float:
        """获取总时长（秒）"""
        return float(self._properties.get("duration", 0))

    def is_paused(self) -> bool:
        """是否暂停"""
        return bool(self._properties.get("pause", False))

    def is_idle(self) -> bool:
        """是否空闲（无文件播放）"""
        return bool(self._properties.get("idle-active", False))

    def get_filename(self) -> str:
        """获取当前文件名"""
        return str(self._properties.get("filename", ""))

    def on_property_change(self, prop: str, callback: Callable[[Any], None]):
        """注册属性变化监听器"""
        self._property_watchers[prop] = callback

    def is_running(self) -> bool:
        """mpv 是否在运行"""
        if not self._running or not self._process:
            return False
        return self._process.poll() is None

    def stop_process(self):
        """停止 mpv 进程"""
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
