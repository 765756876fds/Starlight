"""Side-effect-free dependency checks, usable inside the frozen executable.

No settings, databases, microphone, network, or business services are opened.
"""
import argparse
import importlib
import json
import sys
from pathlib import Path
from .build_info import load_build_info


def collect_report(import_module=importlib.import_module):
    checks = []
    for name in ("PySide6.QtCore", "PySide6.QtWidgets", "pydantic",
                 "pydantic_core", "shazamio_core", "aiohttp", "shazamio"):
        try:
            module = import_module(name)
            if name == "shazamio":
                for symbol in ("Shazam", "Serialize"):
                    if not hasattr(module, symbol):
                        raise ImportError(f"shazamio.{symbol} unavailable")
            checks.append({"component": name, "ok": True})
        except Exception as exc:
            # Exception text may contain local paths or URLs. Keep default
            # shareable reports limited to type and missing module name.
            checks.append({"component": name, "ok": False,
                           "error_type": type(exc).__name__,
                           "missing_module": getattr(exc, "name", None)})
    return {"schema_version": 1, "build": load_build_info(), "frozen": bool(getattr(sys, "frozen", False)),
            "python_version": sys.version.split()[0], "checks": checks,
            "ok": all(check["ok"] for check in checks)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Starlight dependency self-check")
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    report = collect_report()
    # Exclusive creation deliberately preserves previous reports.
    with Path(args.report).open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return 0 if report["ok"] else 1
