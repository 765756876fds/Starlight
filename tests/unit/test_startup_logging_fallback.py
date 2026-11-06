"""Import a real copy of main.py with bounded, process-local logging failures."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = r'''
import importlib.util
import json
import logging
import logging.handlers
import os
from pathlib import Path
import shutil
import sys

source, output, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
output.mkdir()
target = output / "main.py"
shutil.copyfile(source / "main.py", target)
assert target.read_bytes() == (source / "main.py").read_bytes()
sys.path.insert(0, str(source))
from app.core.logger import get_logger
project_logger = get_logger(log_dir=str(output / "project-log"))
from app.core import log_redirect

records, armed, fired = [], [False], [False]
class Recorder(logging.Handler):
    def emit(self, record):
        records.append({"name": record.name, "level": record.levelname,
                        "message": record.getMessage()})
logging.getLogger().handlers.clear()
logging.getLogger().setLevel(logging.INFO)
if mode != "normal":
    logging.getLogger().addHandler(Recorder())

original_install = log_redirect.install_log_redirect
def install(*args, **kwargs):
    value = original_install(*args, **kwargs)
    armed[0] = True
    return value
log_redirect.install_log_redirect = install
original_makedirs = os.makedirs
def makedirs(path, *args, **kwargs):
    if mode == "mkdir" and armed[0] and not fired[0] and Path(path) == output / "logs":
        fired[0] = True
        raise PermissionError("fixture mkdir denied")
    return original_makedirs(path, *args, **kwargs)
os.makedirs = makedirs
if mode == "handler":
    def denied(*args, **kwargs):
        fired[0] = True
        raise PermissionError("fixture handler denied")
    logging.handlers.TimedRotatingFileHandler = denied
elif mode == "basic-config":
    def denied(*args, **kwargs):
        fired[0] = True
        raise RuntimeError("fixture basic-config denied")
    logging.basicConfig = denied

spec = importlib.util.spec_from_file_location("starlight_logging_probe", target)
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
    logging.getLogger("startup_regression").info("STANDARD_LOG_READY")
    for handler in logging.getLogger().handlers:
        handler.flush()
    path = output / "logs/app.log"
    report = {"loaded": hasattr(module, "Application"), "fired": fired[0],
              "project_logger_reused": module.logger is project_logger,
              "records": [r for r in records if "[LogInit]" in r["message"]],
              "file_written": path.exists() and "STANDARD_LOG_READY" in path.read_text("utf8")}
    (output / "result.json").write_text(json.dumps(report), "utf8")
finally:
    log_redirect.close_redirect()
    project_logger.close()
    logging.shutdown()
'''


@pytest.mark.parametrize("mode", ["mkdir", "handler", "basic-config", "normal"])
def test_real_main_import_reports_logging_failure_without_a_secondary_name_error(tmp_path, mode):
    output = tmp_path / "bootstrap"
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", "-c", PROBE, str(ROOT), str(output), mode],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "result.json").read_text("utf-8"))
    assert report["loaded"] and report["project_logger_reused"]
    if mode == "normal":
        assert not report["fired"] and report["records"] == []
        assert report["file_written"]
    else:
        assert report["fired"] and len(report["records"]) == 1
        record = report["records"][0]
        assert record["name"] == "starlight_logging_probe" and record["level"] == "ERROR"
        assert "fixture " + mode + " denied" in record["message"]
        assert "NameError" not in result.stdout + result.stderr
