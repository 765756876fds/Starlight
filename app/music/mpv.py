"""
mpv 控制器 - 通过 JSON IPC (Windows Named Pipe) 控制 mpv

mpv 作为纯音频后端运行，无 GUI，不抢焦点。
Python 通过命名管道发送 JSON 命令控制播放。
"""
import hashlib
import json
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any


class MpvController:
    """mpv JSON IPC 控制器"""

    def __init__(self, mpv_path: str, ipc_pipe: str = "\\\\.\\pipe\\lol_music_mpv",
                 video: bool = False, watch_later_dir: str | None = None,
                 extra_video_args: list | None = None):
        self.mpv_path = mpv_path
        self.ipc_pipe = ipc_pipe
        self._base_ipc_pipe = ipc_pipe  # P0-2：每次 launch 从 base 重建（不追加累积）
        self.video = video
        self.watch_later_dir = watch_later_dir
        self.extra_video_args = extra_video_args or []  # 电影缓冲参数（仅 video 分支追加）
        self._process: subprocess.Popen | None = None
        self._pipe = None
        self._running = False

        self._request_id = 0
        self._properties: dict[str, Any] = {}
        self._property_watchers: dict[str, Any] = {}
        self._pipe_lock = threading.Lock()

    def start(self, taskkill: bool = True, wid: int | None = None,
              pipe_timeout: float = 10.0, prop_timeout: float = 1.0) -> bool:
        """启动 mpv 进程；成功返回 True，失败返回 False（不抛异常）。
        taskkill=True 时先清场（杀残留 mpv.exe，正常冷启动语义）；
        taskkill=False 供自动重启路径（旧进程已被 stop_process 终止，
        再杀全局面板会连带杀掉 TTS 播报进程——必须跳过）。
        wid：内嵌模式传目标窗口 HWND（uint32）→ --wid 参数；None 不传
        （音乐链/独立窗口零影响——验收硬指标）。
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
            # P0-3：不再全局 taskkill（电影/音乐双实例共存，全局杀会杀掉对方实例）。
            # 改为精确终止本实例上次启动的进程（对齐 stop_process 的 terminate/kill）。
            self._kill_own_process()
            time.sleep(0.3)

        return self._launch(wid=wid, pipe_timeout=pipe_timeout, prop_timeout=prop_timeout)

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

    def _launch(self, wid: int | None = None,
                pipe_timeout: float = 10.0, prop_timeout: float = 1.0) -> bool:
        """Popen + 等待 IPC 管道就绪。任何一步失败 → 返回 False（不抛）。"""
        # 旧 pipe 可能来自已自死的进程（窗口关闭路径），先清理再重开
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None
        self.ipc_pipe = self._base_ipc_pipe + "_" + str(os.getpid())

        print(f"[mpv] pipe name: {self.ipc_pipe!r}")
        args = [self.mpv_path]
        if self.video:
            args.append("--force-window=yes")
            args.append("--osc=no")   # 电影：弃用英文自带 OSC，控制走应用面板
            args.append("--hls-bitrate=max")  # 包2c：HLS 强制最高码率（仅视频实例，音乐不加）
            # 电影网络缓冲（包2d）：卡顿优化——预读 15s + 前向缓冲 256MiB + 开局预缓冲
            # + 缓冲后多攒 2s + 断流 15s 快速重试 + 解复用独立线程。
            # 音乐链零影响（仅 video 分支追加）；参数不识别（老 mpv）mpv 会报 warning 忽略，不崩。
            args.extend(self.extra_video_args)
            if wid is not None:
                # 内嵌：渲染到 Qt 容器 HWND（win32 句柄 32 位，负值 mpv 拒收 → 转 uint32）
                args.append("--wid=%d" % (int(wid) & 0xFFFFFFFF))
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
            # 进程创建成功但管道等待/连接失败：进程对象必须保留到清理完成，
            # 否则孤儿 mpv 泄漏（无句柄可终止）。清理完成后统一复位状态。
            print(f"[mpv] 启动失败: {e!r}")
            self._cleanup_failed_launch()
            return False
        self._running = True
        self._init_properties(prop_timeout=prop_timeout)
        print(f"[mpv] 已启动, PID={self._process.pid}")
        return True

    def _cleanup_failed_launch(self):
        """启动失败路径清理：终止本实例刚创建但未就绪的进程（若有），并复位状态。

        - 仅操作 self._process 记录的进程，绝不使用全局 taskkill（电影/音乐双实例共存语义）；
        - terminate() → 有界 wait(3) → 仍存活则 kill()（与 _kill_own_process/stop_process 对齐）；
        - 各清理步骤自身异常不得阻断其余步骤（逐段 try/except）；
        - 完成后统一复位 _process/_pipe/_running；Popen 本身失败时 _process 为 None，直接跳过进程清理。
        """
        if self._process is not None:
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    self._process.wait(timeout=3)
            except Exception:
                pass
            if self._process.poll() is None:
                try:
                    self._process.kill()
                    self._process.wait(timeout=3)
                except Exception:
                    pass
        if self._pipe is not None:
            try:
                self._pipe.close()
            except Exception:
                pass
        self._process = None
        self._pipe = None
        self._running = False

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

    def _send_command(self, command: str, params: list[Any] | None = None,
                       timeout: float = 3.0, *, _locked: bool = False) -> dict[str, Any] | None:
        if not self._pipe:
            raise ConnectionError("mpv IPC 未连接")
        # Only the checkpoint batch below already owns this non-reentrant lock.
        with (nullcontext() if _locked else self._pipe_lock):
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
        # 返回原始响应（与 stop() 三态判定同契约）：调用方需区分
        #   resp.error == "success"（命令被接受）/ resp is None（超时未确认）/
        #   协议层 error 响应（明确失败）。既有调用方忽略返回值，行为不变。
        return resp

    def play(self):
        self.set_property("pause", False)

    def pause(self):
        self.set_property("pause", True)

    def toggle_pause(self):
        self._send_command("cycle", ["pause"])

    def stop(self) -> bool | None:
        """停止当前播放（mpv 回 idle）。

        返回值语义（协议层确认，不代表扬声器已物理静音）：
          True  协议层确认成功（响应 error == "success"）
          False 协议层明确失败（收到错误响应）
          None  结果未确认（IPC 超时 / 无响应 / 管道失效；命令可能已执行或未执行）
        抛异常：IPC 未连接或写入失败（ConnectionError / OSError）。
        """
        resp = self._send_command("stop")
        if resp is None:
            # 超时 / 管道失效：无法确认命令是否被接受
            return None
        if isinstance(resp, dict) and resp.get("error") == "success":
            return True
        # 收到响应但 error != "success" → MPV 明确报告失败
        return False

    def write_watch_later_config(self, expected_path: str, timeout: float = 1.0) -> bool:
        """Save the active local video's native checkpoint before unloading it.

        True: IPC acknowledged and the native file contains the current position.
        False: no eligible media (including idle/EOF/another file/zero position).
        Errors or unconfirmed persistence raise; callers must still stop/close.
        The single IPC budget includes waiting for the pipe lock. Other callers'
        IPC timeout/return semantics remain unchanged.
        """
        if not self.video or not expected_path or not os.path.isfile(expected_path):
            return False
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("checkpoint timeout must be positive and finite")
        deadline = time.monotonic() + timeout
        if not self._pipe_lock.acquire(timeout=timeout):
            raise TimeoutError("watch-later IPC lock deadline exceeded")
        try:
            def rpc(command, params=None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("watch-later IPC deadline exceeded")
                response = self._send_command(command, params, timeout=remaining, _locked=True)
                if response is None:
                    raise TimeoutError("watch-later IPC response unconfirmed")
                if not isinstance(response, dict) or response.get("error") != "success":
                    error = response.get("error") if isinstance(response, dict) else "invalid response"
                    raise RuntimeError(f"watch-later IPC command failed: {command}: {error}")
                return response.get("data")

            if rpc("get_property", ["idle-active"]) is not False:
                return False
            path = rpc("get_property", ["path"])
            if (not isinstance(path, str) or
                    os.path.normcase(os.path.realpath(path)) !=
                    os.path.normcase(os.path.realpath(expected_path))):
                return False
            if rpc("get_property", ["eof-reached"]) is not False:
                return False
            position = rpc("get_property", ["time-pos"])
            if type(position) not in (int, float) or not math.isfinite(position) or position <= 0:
                return False
            position_at = time.monotonic()
            directory = rpc("get_property", ["watch-later-directory"])
            if not isinstance(directory, str) or not directory:
                raise RuntimeError("watch-later directory unavailable")
            # Resolve MPV-specific ~/ and relative paths in the player, not our CWD.
            if not os.path.isabs(directory):
                directory = rpc("expand-path", [directory])
                directory = rpc("normalize-path", [directory])
            if not isinstance(directory, str) or not os.path.isabs(directory):
                raise RuntimeError("watch-later directory could not be resolved")
            ignore_path = rpc("get_property", ["ignore-path-in-watch-later-config"])
            if type(ignore_path) is not bool:
                raise RuntimeError("watch-later filename policy unavailable")
            # MPV's configfiles.c uses uppercase MD5 of the original UTF-8 path.
            key = os.path.basename(path) if ignore_path else path
            name = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
            rpc("write-watch-later-config")
            # MPV can acknowledge success even when its internal file write fails.
            with open(os.path.join(directory, name), encoding="utf-8") as checkpoint:
                starts = [line[6:] for line in checkpoint.read(65536).splitlines()
                          if line.startswith("start=")]
            saved = float(starts[0]) if starts else float("nan")
            tolerance = max(1., 4 * (time.monotonic() - position_at))
            if not math.isfinite(saved) or saved <= 0 or abs(saved - position) > tolerance:
                raise RuntimeError("watch-later position could not be confirmed")
            return True
        finally:
            self._pipe_lock.release()

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

    def set_property(self, name: str, value: Any) -> dict[str, Any] | None:
        """Return the IPC response; None means unconfirmed, not success."""
        return self._send_command("set_property", [name, value])

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

    def get_live_playback_time(self, timeout: float = 1.0) -> float:
        try:
            val = self.get_property("playback-time", timeout=timeout)
            if val is not None:
                self._properties["playback-time"] = val
                return float(val)
        except Exception:
            pass
        return self.get_current_time()

    def get_duration(self, timeout: float = 1.0) -> float:
        try:
            val = self.get_property("duration", timeout=timeout)
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
        # cached 在 MPV 内部按文件名复用已发现的轨道，覆盖自动发现与追加的竞态。
        params = [path_or_url, "cached"]
        if title:
            params.append(title)
        return self._send_command("sub-add", params, timeout=5.0)

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
        return self.set_property("sid", int(sid))

    def on_property_change(self, prop: str, callback: Callable[[Any], None]):
        self._property_watchers[prop] = callback

    def is_running(self) -> bool:
        if not self._running or not self._process:
            return False
        return self._process.poll() is None

    def _kill_own_process(self):
        """精确终止本实例启动的进程（不碰其他 mpv.exe——电影/音乐双实例各管各的）。

        start(taskkill=True) 的旧语义是 taskkill /F /IM mpv.exe 全局清场；
        电影+音乐共用 mpv.exe 名后，全局杀会连带杀掉正在播的另一链实例。
        这里只 terminate/kill self._process 记录的 PID（与 stop_process 对齐）。
        """
        if self._process is None:
            return
        try:
            if self._process.poll() is None:
                self._process.terminate()
                self._process.wait(timeout=3)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass
            try:
                self._process.wait(timeout=3)
            except Exception:
                pass
        self._process = None

    def stop_process(self):
        self._running = False
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None
        process = self._process
        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=3)
                except Exception:
                    pass
            self._process = None
        print("[mpv] 已停止")

    def __del__(self):
        try:
            self.stop_process()
        except Exception:
            pass
