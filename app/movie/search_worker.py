"""One CMS request on stdin, one bounded JSON response on stdout; no user stores."""
import json
from pathlib import Path
import sys
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.movie.cms_parser import normalise_playback, parse_cms_response  # noqa: E402
from app.movie.source_manager import UA, _TEST_KW  # noqa: E402

MAX_BODY = 2_000_000


def fetch(payload):
    started = time.monotonic()
    budget = min(10., max(.01, float(payload["budget"])))
    deadline = started + budget
    query = _TEST_KW if payload.get("mode") == "probe" else payload["keyword"].strip()
    response = None
    try:
        response = requests.get(payload["url"], params={"ac": "videolist", "wd": query},
                                headers=UA, timeout=(min(2., budget), budget), stream=True)
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


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.buffer.read(65537))
        if payload.get("mode") == "verify_switch":
            from app.movie.switch_probe import verify
            result = verify(payload)
        else:
            result = fetch(payload)
    except Exception:
        result = dict(status="failed", items=[], error="WorkerInput")
    output = json.dumps(result, ensure_ascii=False).encode("utf8")
    if len(output) > 4*1024*1024:
        output = b'{"status":"failed","items":[],"error":"ResponseTooLarge"}'
    sys.stdout.buffer.write(output)
