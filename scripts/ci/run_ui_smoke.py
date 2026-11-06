"""Run an explicit offscreen Qt allowlist in isolated, bounded processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
GROUPS = {
    "theme": "tests/ui/test_theme_switching.py",
    "notifications": "tests/ui/test_notification_center.py",
    "movie-cards": "tests/ui/test_movie_card_interactions.py",
    "queue": "tests/ui/test_music_queue_view.py",
}


def child(group: str, output: Path) -> int:
    # Keep strong application/manager references throughout pytest and match main.py's order.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(ROOT))
    from app.core.logger import get_logger
    from app.ui import settings_store

    get_logger(log_dir=str(output / "diagnostic"))
    settings_store.UI_PREFS_PATH = str(output / "prefs.json")
    from PySide6.QtWidgets import QApplication
    from app.ui.theme.manager import get_theme_manager
    import pytest

    application = QApplication.instance() or QApplication([])
    manager = get_theme_manager(application)
    assert manager is not None
    return int(pytest.main([
        GROUPS[group], "-o", "addopts=", "-p", "no:cacheprovider", "-q",
        "--basetemp=" + str(output / "tmp"), "--junitxml=" + str(output / "junit.xml"),
    ]))


def stop_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        process.kill()
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def run_group(group: str, output: Path, timeout: float) -> int:
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPYCACHEPREFIX=str(output / "pycache"))
    command = [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()),
               "--child", "--group", group, "--output-dir", str(output)]
    started = time.monotonic()
    timed_out = False
    with (output / "pytest.log").open("wb") as log:
        try:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                       stderr=subprocess.STDOUT)
        except OSError as error:
            log.write(f"Cannot start test process: {error}\n".encode("utf-8"))
            code = 125
        else:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process_tree(process)
                code = 124
    result = dict(group=group, returncode=code, timed_out=timed_out,
                  seconds=round(time.monotonic() - started, 3))
    # A native crash/timeout may prevent pytest from writing JUnit. Never report it as passed.
    if not (output / "junit.xml").exists():
        suite = ET.Element("testsuite", name="ui-process", tests="1", errors="1")
        case = ET.SubElement(suite, "testcase", name=group)
        ET.SubElement(case, "error", message="Qt test process did not produce JUnit").text = (
            f"returncode={code}; timed_out={timed_out}; see pytest.log"
        )
        ET.ElementTree(suite).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
        if code == 0:
            code = 1
    result["process_returncode"] = result["returncode"]
    result["returncode"] = code
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["all", *GROUPS], default="all")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    output = args.output_dir.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("--output-dir must be outside the checkout")
    if args.child:
        if args.group == "all":
            parser.error("child must select exactly one group")
        return child(args.group, output)
    groups = GROUPS if args.group == "all" else [args.group]
    for group in groups:
        if run_group(group, output / group, args.timeout) != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
