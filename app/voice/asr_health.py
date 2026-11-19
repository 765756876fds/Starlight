"""Read-only readiness checks for the configured ASR server."""
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlsplit

import requests


def model_list_ready(payload, model="default"):
    """A named, ready model. Older direct servers omit readiness/capability metadata."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            continue
        if "meta" in row and row["meta"] is None:
            continue
        if "status" in row:
            status = row["status"]
            if not isinstance(status, dict) or status.get("value") != "loaded":
                continue
            if model not in ("", "default", row["id"]):
                continue
        architecture = row.get("architecture")
        if isinstance(architecture, dict) and "input_modalities" in architecture:
            modalities = architecture["input_modalities"]
            if not isinstance(modalities, list) or "audio" not in modalities:
                continue
        return True
    return False


def read_model_list(base_url, timeout):
    """Read model metadata with a total deadline on the bundled IPv4 loopback path."""
    url = f"{base_url}/models"
    endpoint = urlsplit(url)
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username is not None:
        response = requests.get(url, timeout=timeout)
        return response.status_code, response.json() if response.status_code == 200 else None

    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=timeout)
    watchdog = None
    try:
        connection.connect()
        sock = connection.sock

        def abort():
            # Interrupt even a reply that drips header/body bytes before every read timeout.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ASR health check deadline")
        watchdog = threading.Timer(remaining, abort)
        watchdog.daemon = True
        watchdog.start()
        connection.request("GET", endpoint.path, headers={"Connection": "close"})
        with connection.getresponse() as response:
            if response.status != 200:
                return response.status, None
            limit = 128 * 1024
            body = response.read(limit + 1)
            if len(body) > limit or time.monotonic() >= deadline:
                return response.status, None
            return response.status, json.loads(body)
    finally:
        if watchdog is not None:
            watchdog.cancel()
        connection.close()
