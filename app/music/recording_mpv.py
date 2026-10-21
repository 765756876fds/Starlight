"""RecordingMpv：对 mpv 实例的透明记录代理（影子模式对拍基建）。

- __getattr__ 代理一切属性：未拦截的直接转发原值。
- 重点拦截方法"先调用、再记录"：原方法抛异常原样 re-raise；记录过程自身异常吞掉。
- 脱敏：
  - loadfile 的 filepath：本地路径只记文件名（不记目录）；在线 URL 只记 host 与短 hash。
  - headers 类字典参数：只记包含哪些 key，绝不记录 value。
- 不改变被包装对象的任何行为：仅当 trace_enabled=True 且 Controller 主动包装时生效。
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any

# 需要包装拦截的方法
_WRAPPED = frozenset({
    "get_property",
    "set_property",
    "loadfile",
    "pause",
    "play",
    "toggle_pause",
    "stop",
    "start",
    "stop_process",
    "is_running",
    "is_idle",
    "get_filename",
})

_HTTP_PREFIXES = ("http://", "https://")

# set_property 白名单：只有这些属性记录真实 value（其余只记 key）
_SET_PROPERTY_VALUE_WHITELIST = frozenset({"pause"})


class RecordingMpv:
    def __init__(self, mpv_impl: Any, recorder: Any):
        # 用 object.__setattr__ 规避 __getattr__ 递归
        object.__setattr__(self, "_impl", mpv_impl)
        object.__setattr__(self, "_recorder", recorder)

    # ---- 透明代理 ----

    def __getattr__(self, name: str) -> Any:
        impl = object.__getattribute__(self, "_impl")
        attr = getattr(impl, name)
        if name in _WRAPPED and callable(attr):
            return self._wrap(name, attr)
        return attr

    # ---- 包装 ----

    def _wrap(self, name: str, fn: Any):
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            res = None
            exc = None
            try:
                res = fn(*args, **kwargs)
                return res
            except Exception as e:  # 原样 re-raise，绝不吞业务异常
                exc = self._exc_summary(e)
                raise
            finally:
                dur_ms = round((time.perf_counter() - start) * 1000, 2)
                try:
                    self._record(name, args, kwargs, res, exc, dur_ms)
                except Exception:
                    pass  # 记录失败绝不影响业务
        _wrapper.__name__ = f"recording_{name}"
        return _wrapper

    @staticmethod
    def _exc_summary(e: Exception) -> str:
        """异常文本只记类型名与首行，截断到 100 字符。"""
        msg = str(e)
        first_line = msg.splitlines()[0].strip() if msg else ""
        text = f"{type(e).__name__}: {first_line}" if first_line else type(e).__name__
        if len(text) > 100:
            text = text[:100] + "..."
        return text

    # ---- 记录与脱敏 ----

    def _record(self, name: str, args: tuple, kwargs: dict,
                res: Any, exc: str | None, dur_ms: float) -> None:
        data: dict[str, Any] = {
            "m": name,
            "args": self._sanitize_args(name, args, kwargs),
            "res": self._sanitize_res(name, res),
            "dur_ms": dur_ms,
            "exc": exc,
        }
        if name == "loadfile" and args and isinstance(args[0], str):
            data["file_kind"] = (
                "online" if args[0].startswith(_HTTP_PREFIXES) else "local")
        self._recorder.record("mpv", data)

    @staticmethod
    def _hash_url(url: str) -> str:
        """在线 URL 只留 host + 短 hash：不落完整 URL（防签名参数/令牌泄漏）。"""
        try:
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        except Exception:
            digest = "?"
        try:
            from urllib.parse import urlsplit
            host = urlsplit(url).netloc or "unknown"
        except Exception:
            host = "unknown"
        return f"{host}...{digest}"

    @staticmethod
    def _sanitize_path(fp: str) -> str:
        if fp.startswith(_HTTP_PREFIXES):
            return RecordingMpv._hash_url(fp)
        # 本地路径只记文件名，不记目录
        return os.path.basename(fp)

    @staticmethod
    def _sanitize_headers(headers: dict) -> Any:
        """headers 类参数：只记包含哪些 key，绝不记 value。"""
        if isinstance(headers, dict):
            return {str(k): "<redacted>" for k in headers}
        return headers

    def _sanitize_args(self, name: str, args: tuple, kwargs: dict) -> Any:
        if name == "loadfile":
            parts = [self._sanitize_path(args[0]) if args and isinstance(args[0], str)
                     else self._sanitize_value(args[0]) if args else None]
            parts += [self._sanitize_value(a) for a in args[1:]]
            kws = self._sanitize_kwargs(kwargs)
            return {"args": parts, "kwargs": kws}
        if name == "set_property":
            # set_property(name, value)：除 pause 白名单外，值一律脱敏为 <redacted>
            if args:
                pname, pvalue = args[0], (args[1] if len(args) > 1 else None)
            else:
                pname, pvalue = kwargs.get("name"), kwargs.get("value")
            if pname != "pause":
                pvalue = "<redacted>"
            return {"args": [self._sanitize_value(pname), pvalue], "kwargs": {}}
        return {"args": [self._sanitize_value(a) for a in args],
                "kwargs": self._sanitize_kwargs(kwargs)}

    def _sanitize_kwargs(self, kwargs: dict) -> dict:
        out: dict[str, Any] = {}
        for k, v in kwargs.items():
            if k in ("headers", "extra_headers") or isinstance(v, dict):
                out[str(k)] = self._sanitize_headers(v)
            else:
                out[str(k)] = self._sanitize_value(v)
        return out

    @staticmethod
    def _sanitize_value(v: Any) -> Any:
        if isinstance(v, str):
            if v.startswith(_HTTP_PREFIXES):
                return RecordingMpv._hash_url(v)
            if "\\" in v or v.startswith(("/", "./", "../")):
                return os.path.basename(v)
            if len(v) > 120:
                return v[:120] + "..."
            return v
        return v

    def _sanitize_res(self, name: str, res: Any) -> Any:
        if res is None:
            return None
        if isinstance(res, str):
            return self._sanitize_value(res)
        if isinstance(res, (bool, int, float)):
            return res
        if isinstance(res, dict):
            return {str(k): "<redacted>" for k in res}
        # 其他（对象/列表等）只记类型摘要
        return f"<{type(res).__name__}>"
