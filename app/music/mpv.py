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

    def __init__(self, mpv_path: str, ipc_pipe: str = "\\\\.\\pipe\\lol_music_mpv",
                 video: bool = False, watch_later_dir: Optional[str] = None):
        self.mpv_path = mpv_path
        self.ipc_pipe = ipc_pipe
        self.video = video
        self.watch_later_dir = watch_later_dir
        self._process: Optional[subprocess.Popen] = None
        self._pipe = None
        self._running = False

        self._request_id = 0
        self._properties: Dict[str, Any] = {}
        self._property_watchers: Dict[str, Any] = {}
        self._pipe_lock = threading.Lock()

    def start(self, taskkill: bool = True, pipe_timeout: float = 10.0,
               prop_timeout: float = 1.0) -> bool:
        """启动 mpv 进程；成功返回 True，失败返回 False（不抛异常）。
        taskkill=True 时先清场（杀残留 mpv.exe，正常冷启动语义）；
        taskkill=False 供自动重启路径（旧进程已被 stop_process 终止，
        再杀全局面板会连带杀掉 TTS 播报进程——必须跳过）。
        pipe_timeout/prop_timeout：自动重启路径传短超时（3.0/0.5），
        因为重启在 controller._lock 内执行，持锁时间必须可控。"""
        # 进程真的活着才复用；窗口被用户关闭（进程自死，poll() 非 None）时
        # _running 标志仍 True，但必须重新 launch——否则 start 被挡死、无法唤醒。
        if (self._running and self._process is not None
                and self._process.poll() is None):
            return True

        if not self._resolve_mpv_path():
            return False

        if taskkill:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "mpv.exe"],
                    capture_output=True, timeout=5,
                )
                time.sleep(0.3)
            except Exception:
                pass

        return self._launch(pipe_timeout=pipe_timeout, prop_timeout=prop_timeout)

    def _resolve_mpv_path(self) -> bool:
        """解析 mpv 可执行文件：先配置路径（isfile），再 PATH（shutil.which）。
        旧的 os.path.exists("mpv") 只查当前目录相对路径，PATH 里有 mpv.exe 也会误报。"""
        if os.path.isfile(self.mpv_path):
            return True
        import shutil
        found = shutil.which(self.mpv_path)
        if found:
            self.mpv_path = found
            return True
        print(f"[mpv] 未找到 mpv 可执行文件: {self.mpv_path}")
        return False

    def _launch(self, pipe_timeout: float = 10.0, prop_timeout: float = 1.0) -> bool:
        """Popen + 等待 IPC 管道就绪。任何一步失败 → 返回 False（不抛）。"""
        # 旧 pipe 可能来自已自死的进程（窗口关闭路径），先清理再重开
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None
        self.ipc_pipe = self.ipc_pipe + "_" + str(os.getpid())

        print(f"[mpv] pipe name: {self.ipc_pipe!r}")
        args = [self.mpv_path]
        if self.video:
            args.append("--force-window=yes")
            args.append("--osc=no")   # 电影：弃用英文自带 OSC，控制走应用面板
            args.append("--hls-bitrate=max")  # 包2c：HLS 强制最高码率（仅视频实例，音乐不加）
            if self.watch_later_dir:
                args.append("--save-position-on-quit")
                args.append(f"--watch-later-directory={self.watch_later_dir}")
        else:
            args.append("--no-video")
            args.append("--force-window=no")
        args += [
            "--no-terminal",
            f"--input-ipc-server={self.ipc_pipe}",
            "--idle=yes",
            "--tls-verify=no",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        ]

        try:
            self._process = subprocess.Popen(
                args,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            self._wait_for_pipe(timeout=pipe_timeout)
        except Exception as e:
            print(f"[mpv] 启动失败: {e!r}")
            self._process = None
            self._pipe = None
            self._running = False
            return False
        self._running = True
        self._init_properties(prop_timeout=prop_timeout)
        print(f"[mpv] 已启动, PID={self._process.pid}")
        return True

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

    def _init_properties(self, prop_timeout: float = 1.0):
        properties = ["pause", "volume", "idle-active", "filename", "playback-time", "duration"]
        for prop in properties:
            try:
                value = self.get_property(prop, timeout=prop_timeout)
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
                # Windows 命名管道有界读：PeekNamedPipe 探测可读字节数，
                # 无数据则短眠轮询；mpv 不响应时到点强制返回 None，绝不无限阻塞。
                avail = self._pipe_available()
                if avail < 0:
                    break
                if avail <= 0:
                    time.sleep(0.01)
                    continue
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

    def _pipe_available(self) -> int:
        """探测命名管道当前可读字节数。

        返回：
          >0  有数据可读（readline 不会阻塞）
          0   当前无数据（调用方短眠轮询）
          -1  管道已断/失效（调用方立即返回）
        无法探测（非 Windows 管道 / mock 测试环境）时乐观返回 1，
        保持旧的阻塞 readline 语义。
        """
        try:
            import ctypes
            import msvcrt
            fd = self._pipe.fileno()
            if not isinstance(fd, int):
                # mock/非真实管道（如测试环境）：乐观可读，保持旧语义
                return 1
            handle = msvcrt.get_osfhandle(fd)
            total = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(total), None)
            if not ok:
                return -1
            return total.value
        except Exception:
            return 1

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

    def get_video_params(self) -> dict:
        """实时查 video-params（w/h/编码等）；未运行或失败 → {}（面板显示分辨率用）。"""
        if not self.is_running():
            return {}
        try:
            val = self.get_property("video-params", timeout=1.0)
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}

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

    def get_idle_active(self) -> bool:
        """实时查询 idle-active（不读启动时缓存——音乐链教训：_properties 不随播放更新）。"""
        try:
            val = self.get_property("idle-active", timeout=1.0)
            if val is not None:
                self._properties["idle-active"] = bool(val)
                return bool(val)
        except Exception:
            pass
        return bool(self._properties.get("idle-active", False))

    # ---- 字幕 ----

    def sub_add(self, path_or_url: str, title: str = ""):
        """加载外部字幕（本地路径或 http URL）。必须在 loadfile 之后调用
        （idle 无文件态 sub-add 报 error running command——已实测）。"""
        params = [path_or_url]
        if title:
            params.append("select")
            params.append(title)
        self._send_command("sub-add", params, timeout=5.0)

    def get_track_list(self) -> list:
        """实时 track-list（找字幕轨 id 用）。"""
        try:
            val = self.get_property("track-list", timeout=2.0)
            if isinstance(val, list):
                return val
        except Exception:
            pass
        return []

    def set_sid(self, sid: int):
        """强制选中指定字幕轨（sub-add 后 selected 可能 False——已实测，必须显式选）。"""
        self.set_property("sid", int(sid))

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
