"""Python side of JS music source bridge. Spawns node bridge.js over stdio NDJSON."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import List, Optional

from ..models import OnlineTrack
from ..source import MusicSource


class JSMusicSource(MusicSource):
    name = "js_bridge"
    display_name = "JS 音源桥接"

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
                self._ready.set()
                continue
            with self._lock:
                box = self._pending.pop(rid, None)
            if box:
                box["result"] = msg
                box["event"].set()

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
            with self._lock:
                self._pending.pop(rid, None)
            return None
        msg = box["result"]
        if not msg or not msg.get("ok"):
            return None
        return msg.get("result")

    def list_sources(self) -> List[str]:
        return list(self._sources)

    def search(self, keyword: str, limit: int = 20) -> List[OnlineTrack]:
        if not keyword:
            return []
        result = self._call("search", {"source": "*", "keyword": keyword, "limit": limit})
        if not result:
            return []
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

    def get_play_url(self, track: OnlineTrack) -> Optional[dict]:
        r = self._call("getUrl", {
            "source": track.source_name,
            "track_id": track.track_id,
            "quality": "320k",
            "raw_item": track.extra.get("_raw"),
        })
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
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
