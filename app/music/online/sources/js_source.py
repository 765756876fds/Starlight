"""Python side of JS music source bridge. Spawns node bridge.js over stdio NDJSON."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from ..models import OnlineTrack
from ..source import MusicSource


def compare_versions(a, b):
    """版本号比较：拆整数逐位比，长度不齐补 0，剥 v/V 前缀。

    返回 1（a>b）/ 0（相等）/ -1（a<b）；解析失败返回 None（视为"未知"，
    调用方只把 >0 当"有更新"，None 走"未知"不提示更新）。
    """
    if not a or not b:
        return None

    def _parts(s):
        s = str(s).strip().lstrip("vV")
        parts = []
        for seg in s.split("."):
            seg = seg.strip()
            if not seg or not seg.isdigit():
                return None
            parts.append(int(seg))
        return parts

    pa, pb = _parts(a), _parts(b)
    if pa is None or pb is None:
        return None
    n = max(len(pa), len(pb))
    for i in range(n):
        x = pa[i] if i < len(pa) else 0   # 长度不齐补 0：1 vs 1.0.0 → 相等
        y = pb[i] if i < len(pb) else 0
        if x > y:
            return 1
        if x < y:
            return -1
    return 0


class JSMusicSource(MusicSource):
    name = "js_bridge"
    display_name = "JS 音源桥接"

    # --- watchdog 状态（类属性默认：__new__ 构造的测试对象不初始化 __init__ 字段）---
    _call_timeout_streak = 0
    _auto_reload_count = 0
    _auto_reload_window_ts = 0.0
    _auto_reloading = False
    _auto_reload_gave_up = False
    _closed = False

    def __init__(self, mf_dir: str, lx_dir: str, bridge_dir: Optional[str] = None,
                 user_variables: Optional[dict] = None):
        self._mf_dir = str(Path(mf_dir).resolve())
        self._lx_dir = str(Path(lx_dir).resolve())
        self._user_variables = user_variables or {}
        self._bridge_dir = Path(bridge_dir) if bridge_dir else (
            Path(__file__).parent.parent / "js_bridge"
        )
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._sources: List[str] = []
        self._plugins: List[dict] = []   # [{name, type, status, error?}]
        self._ready = threading.Event()
        self._start_bridge()
        # 阻塞等待 bridge 就绪
        self._ready.wait(timeout=3.0)

    def _find_node(self):
        import shutil
        candidates = []
        env_p = os.environ.get("NODE_EXE", "")
        if env_p:
            candidates.append(env_p)
        candidates += [
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
            r"D:\nodejs\node.exe",
            r"D:\Program Files\nodejs\node.exe",
            r"C:\nvm4w\nodejs\node.exe",
            os.path.expanduser(r"~\scoop\apps\nodejs\current\node.exe"),
            os.path.expanduser(r"~\scoop\shims\node.exe"),
            os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
            os.path.expanduser(r"~\AppData\Roaming\npm\node.exe"),
            r"C:\tools\nodejs\node.exe",
        ]
        which = shutil.which("node")
        if which:
            candidates.append(which)
        for c in candidates:
            if c and os.path.isfile(c):
                print(f"[JSBridge] 使用 node: {c}")
                return c
        # nvm 扫描
        try:
            nvm_root = os.path.expanduser(r"~\AppData\Roaming\nvm")
            if os.path.isdir(nvm_root):
                for ver in sorted(os.listdir(nvm_root), reverse=True):
                    cand = os.path.join(nvm_root, ver, "node.exe")
                    if os.path.isfile(cand):
                        print(f"[JSBridge] 使用 node (nvm): {cand}")
                        return cand
        except Exception:
            pass
        print("[JSBridge] 未找到 node.exe。请安装 Node.js 或设置环境变量 NODE_EXE。")
        return None

    def _start_bridge(self) -> None:
        bridge_js = self._bridge_dir / "bridge.js"
        if not bridge_js.exists():
            print(f"[JSBridge] bridge.js 不存在: {bridge_js}")
            return
        node = self._find_node()
        if not node:
            print("[JSBridge] 未找到 node.exe，在线音乐 JS 源不可用")
            return
        env = os.environ.copy()
        env["MF_PLUGIN_DIR"] = self._mf_dir
        env["LX_PLUGIN_DIR"] = self._lx_dir
        if self._user_variables:
            env["MF_USER_VARIABLES"] = json.dumps(self._user_variables, ensure_ascii=False)
        try:
            self._proc = subprocess.Popen(
                [node, str(bridge_js)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._bridge_dir),
                env=env,
                text=True, encoding="utf-8", bufsize=1,
            )
        except Exception as e:
            print(f"[JSBridge] 启动失败: {e}")
            self._proc = None
            return
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._err_loop, daemon=True).start()

    def _err_loop(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            print(f"[JSBridge] {line.rstrip()}", flush=True)

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            rid = msg.get("id")
            if rid == "_ready":
                self._sources = msg.get("sources", [])
                self._plugins = msg.get("plugins", [])
                self._ready.set()
                continue
            with self._lock:
                box = self._pending.pop(rid, None)
            if box:
                box["result"] = msg
                box["event"].set()
        # for 循环正常退出 = node 进程退出 / stdout EOF → 触发自动恢复
        print("[JSBridge] bridge 进程退出，尝试自动恢复")
        self._maybe_auto_reload("node-exit")

    def _call(self, method: str, params: dict, timeout: float = 15.0):
        if self._proc is None:
            return None
        rid = str(uuid.uuid4())
        req = {"id": rid, "method": method, "params": params}
        with self._lock:
            box = {"event": threading.Event(), "result": None}
            self._pending[rid] = box
        try:
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception:
            return None
        if not box["event"].wait(timeout):
            print(f"[JSBridge] TIMEOUT {method} {params.get('source')} 等了{timeout}s", flush=True)
            with self._lock:
                self._pending.pop(rid, None)
            self._note_call_timeout()
            return None
        msg = box["result"]
        if not msg or not msg.get("ok"):
            print(f"[JSBridge] FAIL {method} {params.get('source')} resp={msg}", flush=True)
            return None
        self._call_timeout_streak = 0
        return msg.get("result")

    @property
    def available(self) -> bool:
        """bridge 实际可用：node 进程存活且 ready 已就绪（构造成功 ≠ 可用）。"""
        try:
            return self._proc is not None and self._ready.is_set()
        except Exception:
            return False

    def list_sources(self) -> List[str]:
        return list(self._sources)

    def list_plugins(self) -> List[dict]:
        """结构化插件列表：[{name, type(musicfree/lx), status(ok/fail), error?}]。
        供设置页「内置插件」UI 展示。"""
        return [dict(p) for p in self._plugins]

    def accepts(self, track) -> bool:
        """track.source_name 是否本源内部插件名（"酷狗音乐"等）或注册名。
        manager 定向 miss 时优先命中本源，避免遍历 http 源发无谓请求。
        _plugins 由 _ready 消息填充（bridge 启动即填）；空/未知时放行，
        让 bridge 自行按插件名路由（正常路径总能匹配）。"""
        if not track or not track.source_name:
            return False
        if track.source_name == self.name:      # "js_bridge" 自身
            return True
        try:
            return any(p.get("name") == track.source_name for p in self._plugins)
        except Exception:
            return True   # _plugins 元素异常时放行（bridge 仍能按名路由）

    def search_plugin(self, source: str, keyword: str, limit: int = 20) -> List[OnlineTrack]:
        """只搜指定插件（测试按钮用，不经 manager 遍历回退）。"""
        if not keyword or not source:
            return []
        print(f"[search_plugin] -> {source} kw={keyword} limit={limit}", flush=True)
        result = self._call("search", {"source": source, "keyword": keyword, "limit": limit})
        print(f"[search_plugin] <- {source} raw={None if result is None else len(result)}", flush=True)
        if not result:
            return []
        out = self._parse_tracks(result)
        print(f"[search_plugin] parsed {len(out)} 条", flush=True)
        return out

    # ---------- 插件检查更新 / 更新 ----------

    def check_updates(self, timeout: float = 25.0) -> List[dict]:
        """拉全部 MusicFree 插件的更新源，返回判断结果列表。

        每项：{name, file, local_version, remote_version, has_update, error?}
        新代码缓存在 _updates_cache（按 name），供 update_plugin 使用，不透传 UI。
        """
        self._updates_cache = {}
        result = self._call("checkUpdates", {"timeout_ms": 10000}, timeout=timeout)
        if not result:
            return []
        out = []
        for r in result:
            name = r.get("name") or r.get("file") or "?"
            error = r.get("error")
            entry = {
                "name": name,
                "file": r.get("file") or "",
                "local_version": r.get("localVersion"),
                "remote_version": r.get("remoteVersion"),
                "error": error,
            }
            if error:
                entry["has_update"] = False
            else:
                cmp = compare_versions(r.get("remoteVersion"), r.get("localVersion"))
                entry["has_update"] = bool(cmp and cmp > 0)
                if cmp is None:
                    entry["error"] = "version-unknown"
            if r.get("newCode"):
                self._updates_cache[name] = r
            out.append(entry)
        return out

    def update_plugin(self, name: str) -> dict:
        """按用户确认更新单插件：备份旧版 → 原子写新版（本地文件名不变）→ reload bridge。

        返回 {ok: True} 或 {ok: False, error}。
        """
        entry = (self._updates_cache or {}).get(name)
        if not entry or not entry.get("newCode"):
            return {"ok": False, "error": "no-cache"}
        mf_dir = Path(self._mf_dir)
        target = mf_dir / (entry.get("file") or "")
        if not target.exists():
            return {"ok": False, "error": f"missing {target.name}"}
        try:
            # 1) 备份旧版到 _trash/plugin_backup/<本地名>.<旧版本>
            backup_dir = mf_dir.parent / "_trash" / "plugin_backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            ver = entry.get("local_version") or "unknown"
            backup_path = backup_dir / f"{target.name}.{ver}"
            import shutil
            shutil.copy2(target, backup_path)
            print(f"[JSBridge] 备份旧版: {target.name} -> {backup_path}", flush=True)
            # 2) 原子写新版（tmp + os.replace，文件名不变）
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(entry["newCode"], encoding="utf-8")
            os.replace(tmp, target)
            print(f"[JSBridge] 已替换: {target.name} (v{entry.get('local_version')}"
                  f" -> v{entry.get('remote_version')})", flush=True)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # 3) reload bridge（新代码生效；短暂中断搜索，预期行为）
        ok = self.reload()
        self._updates_cache.pop(name, None)
        return {"ok": ok, "error": None if ok else "reload-fail"}

    def delete_plugin(self, name: str) -> dict:
        """删除插件文件并 reload bridge（清理 X 状态/不想要的插件）。

        定位顺序：① _plugins 里该 name 的 file 字段（bridge 上报，成功/失败插件都有）；
        ② lx 插件兜底 lx_dir/{name}.js（lx name=basename，中文文件名直接拼）。
        只允许删 mf_dir / lx_dir 内的 .js（relative_to 判定，防前缀误判/路径穿越）。
        删除后 reload 生效（bridge 重扫，列表自动刷新）。
        返回 {ok: True} 或 {ok: False, error: str}。
        """
        try:
            target = None
            for p in self._plugins:
                if p.get("name") == name:
                    f = p.get("file")
                    if f:
                        target = Path(f)
                    break
            if target is None:
                cand = Path(self._lx_dir) / f"{name}.js"
                if cand.exists():
                    target = cand
            if target is None or not target.exists():
                return {"ok": False, "error": f"找不到插件文件: {name}"}
            if not target.is_file() or target.suffix.lower() != ".js":
                return {"ok": False, "error": f"拒绝删除非插件文件: {target.name}"}
            allowed = {Path(self._mf_dir).resolve(), Path(self._lx_dir).resolve()}
            real = target.resolve()
            in_allowed = False
            for d in allowed:
                try:
                    real.relative_to(d)
                    in_allowed = True
                    break
                except ValueError:
                    continue
            if not in_allowed:
                return {"ok": False, "error": f"拒绝删除音源目录外文件: {real}"}
            print(f"[JSBridge] 删除插件: {real}", flush=True)
            os.remove(real)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        ok = self.reload()
        return {"ok": ok, "error": None if ok else "reload-fail"}

    def _parse_tracks(self, result: list) -> List[OnlineTrack]:
        out: List[OnlineTrack] = []
        for r in result:
            try:
                out.append(OnlineTrack(
                    source_name=r.get("source_name", self.name),
                    track_id=str(r.get("track_id", "")),
                    title=r.get("title", ""),
                    artist=r.get("artist", ""),
                    album=r.get("album", ""),
                    duration_seconds=int(r.get("duration_seconds") or 0),
                    extra=r.get("extra") or {},
                ))
            except Exception:
                continue
        return out

    def reload(self) -> bool:
        """重新扫描插件目录：终止旧 bridge 进程并重启，等待新的 _ready。

        - 进行中的 RPC（_pending）在旧进程终止后永远等不到响应：主动标记失败并
          set event，避免调用方傻等 15s 超时（reload 会短暂中断搜索，预期行为）。
        - 调用方应防抖（低频操作，连续点击只跑一次）。
        """
        with self._lock:
            stale = list(self._pending.values())
            self._pending.clear()
        for box in stale:
            box["result"] = None
            box["event"].set()

        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        self._sources = []
        self._plugins = []
        self._ready.clear()
        self._start_bridge()
        ok = self._ready.wait(timeout=8.0)
        return ok and self._proc is not None

    def _note_call_timeout(self):
        """RPC 超时计数：连续 3 次超时（≈45s 无响应）→ 触发自动 reload 探测。
        单次超时不动作（网络慢/插件请求久属正常）。"""
        self._call_timeout_streak += 1
        if self._call_timeout_streak >= 3:
            self._call_timeout_streak = 0
            self._maybe_auto_reload("call-timeout")

    def _maybe_auto_reload(self, reason: str):
        """自动恢复：node 退出（EOF 快速路径）或连续超时（卡死慢路径）→ reload 一次。
        上限 3 次/5 分钟；超过 → 放弃自动重启（_auto_reload_gave_up）。"""
        if self._closed:
            return
        if self._auto_reloading:
            return
        now = time.time()
        if now - self._auto_reload_window_ts > 300:
            self._auto_reload_window_ts = now
            self._auto_reload_count = 0
        if self._auto_reload_count >= 3:
            if not self._auto_reload_gave_up:
                self._auto_reload_gave_up = True
                print(f"[JSBridge] 自动恢复失败 {self._auto_reload_count} 次，放弃自动重启（可手动 reload）")
            return
        self._auto_reloading = True
        try:
            ok = self.reload()
            print(f"[JSBridge] 自动恢复({reason}) {'成功' if ok else '失败'}")
            if ok:
                self._auto_reload_gave_up = False
        except Exception as e:
            print(f"[JSBridge] 自动恢复异常: {e!r}")
        finally:
            self._auto_reloading = False

    def search(self, keyword: str, limit: int = 20) -> List[OnlineTrack]:
        if not keyword:
            return []
        result = self._call("search", {"source": "*", "keyword": keyword, "limit": limit})
        if not result:
            return []
        return self._parse_tracks(result)

    def get_play_url(self, track: OnlineTrack) -> Optional[dict]:
        print(f"[getUrl] -> {track.source_name} track_id={track.track_id}", flush=True)
        r = self._call("getUrl", {
            "source": track.source_name,
            "track_id": track.track_id,
            "quality": "high",   # MusicFree 标准音质键（'320k' 非标准，kg/wy 会直接失败）
            "raw_item": track.extra.get("_raw"),
        })
        print(f"[getUrl] <- {track.source_name} raw={None if r is None else 'dict'}", flush=True)
        if not r:
            return None
        # 返回 dict（url + 插件透传的 headers），供播放链按需设置 http-header-fields
        return {
            "url": r.get("url"),
            "headers": r.get("headers") or None,
        }

    def get_lyric(self, track: OnlineTrack) -> Optional[str]:
        r = self._call("getLyric", {
            "source": track.source_name,
            "track_id": track.track_id,
            "raw_item": track.extra.get("_raw"),
        })
        if not r:
            return None
        return r.get("lyric")

    def close(self) -> None:
        self._closed = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
