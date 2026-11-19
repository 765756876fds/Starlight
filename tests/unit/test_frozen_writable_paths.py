"""Exercise startup path selection in a fresh interpreter, without using host settings."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("existing", [False, True])
def test_frozen_paths_with_foreign_cwd(tmp_path, existing):
    install = tmp_path / "program"
    install.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    local = tmp_path / "local"
    check = r'''
import os
from pathlib import Path
import sys
import yaml
sys.frozen = True
sys.executable = str(Path(sys.argv[1]) / "Star.exe")
user = Path(os.environ["LOCALAPPDATA"]) / "Starlight"
cfg = user / "config" / "settings.yaml"
if sys.argv[2] == "True":
    cfg.parent.mkdir(parents=True)
    cfg.write_text("app: {log_dir: ./custom}\nmovie: {sources: [{name: A, url: 'http://localhost/a'}]}\nkeep: preserved\n")
from app.core.path_resolve import resolve_user_path
assert Path(resolve_user_path(str(Path(sys.argv[1]) / "_internal/data/events"))) == user / "data/events"
assert Path(resolve_user_path("../data/history.db")) == user / "data/history.db"
from app.core.logger import get_logger
early = get_logger()
early.info("early")
from app.core.config import Config
config = Config()
assert cfg.is_file()
directory = config.get("app.log_dir")
late = get_logger(directory, "DEBUG")
assert late is early
late.debug("configured")
late.close()
expected = "custom" if sys.argv[2] == "True" else "logs"
assert Path(directory) == user / expected
assert "configured" in next((user / expected).glob("*.jsonl")).read_text("utf8")
from app.lol.ongoing.debug_tracer import ongoing_logger
assert Path(ongoing_logger.handlers[0].baseFilename).parent == user / "logs"
from app.movie.source_manager import MovieSourceManager
manager = MovieSourceManager()
if sys.argv[2] == "True":
    assert manager.get_active_name() == "A"
manager.add_source("B", "http://localhost/b")
manager.set_active("B")
assert MovieSourceManager().get_active_name() == "B"
if sys.argv[2] == "True":
    assert yaml.safe_load(cfg.read_text("utf8"))["keep"] == "preserved"
assert not list(Path(sys.argv[1]).iterdir())
assert not list(Path.cwd().iterdir())
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", check, str(install), str(existing)],
        env={**os.environ, "LOCALAPPDATA": str(local),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        cwd=cwd, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
