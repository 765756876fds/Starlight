"""One CMS request on stdin, one bounded JSON response on stdout; no user stores."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import json
import os
import sys
import time

from .cms_parser import CMS_HEADERS, CMS_PROBE_KEYWORD, normalise_playback, parse_cms_response

MAX_INPUT = 64 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_BODY = 2_000_000


def fetch(payload):
    import requests

    started = time.monotonic()
    budget = min(10., max(.01, float(payload["budget"])))
    deadline = started + budget
    query = CMS_PROBE_KEYWORD if payload.get("mode") == "probe" else payload["keyword"].strip()
    response = None
    try:
        response = requests.get(payload["url"], params={"ac": "videolist", "wd": query},
                                headers=CMS_HEADERS, timeout=(min(2., budget), budget), stream=True)
        response.raise_for_status()
        body = bytearray()
        while time.monotonic() < deadline:
            chunk = response.raw.read1(65536, decode_content=True)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise ValueError("ResponseTooLarge")
        if time.monotonic() >= deadline:
            raise requests.Timeout()
        data = parse_cms_response(body.decode(response.encoding or "utf-8-sig", errors="replace"))
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError("InvalidCMS")
        rows = []
        for raw in data["list"][:500]:
            if not isinstance(raw, dict):
                raise ValueError("InvalidCMSRow")
            item = dict(raw, play_url=raw.get("vod_play_url") or "", first_url="", first_name="")
            item.pop("_playback_line_index", None)  # Only a local user choice can select a line.
            item = normalise_playback(item)
            item.setdefault("first_url", "")
            item.setdefault("first_name", "")
            rows.append(item)
        return dict(status="ok" if rows else "empty", items=rows if payload.get("mode") != "probe" else [],
                    count=len(data["list"]), limited=len(data["list"]) > 500,
                    http_status=response.status_code, elapsed_ms=round((time.monotonic()-started)*1000))
    except Exception as exc:
        # Never return exception messages (requests exceptions can contain private URLs).
        category = str(exc) if type(exc) is ValueError and str(exc) in (
            "InvalidCMS", "InvalidCMSRow", "ResponseTooLarge") else type(exc).__name__
        return dict(status="failed", items=[], error=category,
                    http_status=response.status_code if response is not None else None,
                    elapsed_ms=round((time.monotonic()-started)*1000))
    finally:
        if response is not None:
            response.close()


def _pipe(stack, stream, *, reading):
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        return binary
    if os.name != "nt":
        raise OSError("missing worker stream")

    # PyInstaller windowed builds may leave the Python streams unset even
    # with QProcess pipes attached to the OS standard handles.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_handle = kernel.GetStdHandle
    get_handle.argtypes = [wintypes.DWORD]
    get_handle.restype = wintypes.HANDLE
    file_type = kernel.GetFileType
    file_type.argtypes = [wintypes.HANDLE]
    file_type.restype = wintypes.DWORD
    handle = get_handle(-10 if reading else -11)
    if not handle or file_type(handle) != 3:  # FILE_TYPE_PIPE
        raise OSError("missing worker pipe")
    mode = os.O_RDONLY if reading else os.O_WRONLY
    fd = msvcrt.open_osfhandle(handle, mode | os.O_BINARY | os.O_NOINHERIT)
    return stack.enter_context(os.fdopen(fd, "rb" if reading else "wb"))


def main():
    try:
        with ExitStack() as stack:
            reader = _pipe(stack, sys.stdin, reading=True)
            writer = _pipe(stack, sys.stdout, reading=False)
            quiet = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            with redirect_stdout(quiet), redirect_stderr(quiet):
                output = _run_one(reader)
            writer.write(output)
            writer.flush()
    except OSError:
        return 2
    return 0


def _run_one(reader):
    try:
        raw = reader.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("oversized job")
        payload = json.loads(raw)
        mode = payload["mode"]
        if mode == "verify_switch":
            from .switch_probe import verify
            result = verify(payload)
        elif mode in ("search", "probe"):
            result = fetch(payload)
        else:
            raise ValueError("unexpected job mode")
        output = json.dumps(result, ensure_ascii=False).encode("utf-8")
    except Exception:
        return b'{"status":"failed","items":[],"error":"WorkerInput"}'
    if len(output) > MAX_OUTPUT:
        return b'{"status":"failed","items":[],"error":"ResponseTooLarge"}'
    return output
