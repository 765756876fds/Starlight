"""
mpv 控制器 - 通过 JSON IPC (Windows Named Pipe) 控制 mpv

mpv 作为纯音频后端运行，无 GUI，不抢焦点。
Python 通过命名管道发送 JSON 命令控制播放。

架构：双连接
- 写连接：主线程发送控制命令
- 读连接：单独线程读取事件和响应

每个连接只在一个线程中使用，避免 Python 文件对象的线程安全问题。
"""
import json
import os
import queue
import subprocess
import threading
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
        self._write_pipe = None  # 主线程写入
        self._read_pipe = None   # 读线程读取
        self._running = False

        # 事件线程相关
        self._read_thread: Optional[threading.Thread] = None
        self._request_id = 0
        self._properties: Dict[str, Any] = {}
        self._property_watchers: Dict[str, Callable] = {}
        self._event_listeners: List[Callable[[Dict[str, Any]], None]] = []

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

        # 等待管道就绪并建立连接
        self._wait_for_pipe()
        self._running = True

        # 启动读线程（使用读连接）
        self._read_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="mpv-read"
        )
        self._read_thread.start()

        # 在读连接上观察常用属性（这样事件会发送回读连接）
        # 注意：必须在读连接上发送 observe_property，
        # 否则事件会发送回写连接，读连接收不到
        self._observe_properties_via_read_pipe()

        print(f"[mpv] 已启动, PID={self._process.pid}")

    def _wait_for_pipe(self, timeout: float = 5.0):
        """
        等待命名管道就绪，并建立两个连接

        先用 os.path.exists() 轮询等待管道出现，
        然后打开两个独立连接：一个写、一个读。
        """
        start = time.time()

        # 先等待管道文件出现（轮询）
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

        # 打开写连接（主线程使用）
        self._write_pipe = open(self.ipc_pipe, "w", buffering=1, encoding="utf-8")
        time.sleep(0.1)

        # 打开读连接（读线程使用，读写模式以便发送 observe_property）
        self._read_pipe = open(self.ipc_pipe, "r+", buffering=1, encoding="utf-8")

    def _observe_properties_via_read_pipe(self):
        """在读连接上观察常用属性变化"""
        properties = [
            "playback-time", "duration", "pause", "volume",
            "filename", "media-title", "idle-active",
            "eof-reached", "playlist-pos",
        ]
        for prop in properties:
            req_id = self._request_id
            self._request_id += 1
            message = {
                "command": ["observe_property", req_id, prop],
                "request_id": req_id,
            }
            line = json.dumps(message) + "\n"
            try:
                self._read_pipe.write(line)
                self._read_pipe.flush()
            except Exception as e:
                print(f"[mpv] observe_property 发送失败: {e}")

    def _read_loop(self):
        """读线程主循环（使用读连接）"""
        while self._running and self._read_pipe:
            try:
                line = self._read_pipe.readline()
                if not line:
                    time.sleep(0.05)
                    continue
                line = line.strip()
                if line:
                    try:
                        message = json.loads(line)
                        self._handle_message(message)
                    except json.JSONDecodeError:
                        pass
            except (OSError, ValueError):
                if self._running:
                    time.sleep(0.1)

    def _handle_message(self, message: Dict[str, Any]):
        """处理 mpv 消息"""
        if "event" in message:
            event = message["event"]
            if event == "property-change":
                prop = message.get("name")
                value = message.get("data")
                if prop:
                    self._properties[prop] = value
                    # 通知属性监听器
                    if prop in self._property_watchers:
                        try:
                            self._property_watchers[prop](value)
                        except Exception:
                            pass
        # 通知事件监听器
        for listener in self._event_listeners:
            try:
                listener(message)
            except Exception:
                pass

    def _send_command(self, command: str, params: Optional[List[Any]] = None) -> Any:
        """
        发送命令到 mpv（使用写连接）

        Args:
            command: 命令名，如 "loadfile", "set_property"
            params: 参数列表

        Returns:
            命令执行结果
        """
        req_id = self._request_id
        self._request_id += 1

        message = {
            "command": [command] + (params or []),
            "request_id": req_id,
        }

        line = json.dumps(message) + "\n"
        self._write_pipe.write(line)
        self._write_pipe.flush()

        return None

    def loadfile(self, filepath: str, mode: str = "replace"):
        """加载并播放文件"""
        self._send_command("loadfile", [filepath, mode])

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
        """
        跳转

        Args:
            seconds: 秒数
            mode: "relative" 相对, "absolute" 绝对, "absolute-percent" 百分比
        """
        self._send_command("seek", [seconds, mode])

    def set_volume(self, volume: int):
        """设置音量 0-100"""
        volume = max(0, min(100, volume))
        self.set_property("volume", float(volume))

    def get_volume(self) -> int:
        """获取音量"""
        return int(self._properties.get("volume", 0))

    def set_property(self, name: str, value: Any):
        """设置属性"""
        self._send_command("set_property", [name, value])

    def get_property(self, name: str) -> Any:
        """获取属性（从缓存）"""
        return self._properties.get(name)

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

    def add_event_listener(self, listener: Callable[[Dict[str, Any]], None]):
        """添加事件监听器"""
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable):
        """移除事件监听器"""
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

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

        if self._write_pipe:
            try:
                self._write_pipe.close()
            except Exception:
                pass
            self._write_pipe = None

        if self._read_pipe:
            try:
                self._read_pipe.close()
            except Exception:
                pass
            self._read_pipe = None

        if self._read_thread:
            self._read_thread.join(timeout=1)
            self._read_thread = None

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
