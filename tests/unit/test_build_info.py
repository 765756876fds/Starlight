import json
import subprocess
from app.core import build_info


def test_manifest_tracks_uncommitted_source_not_user_data(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "main.py").write_text("print('a')")
    git("add", "main.py")
    git("commit", "-m", "test")
    first = build_info.create_manifest(tmp_path)
    assert first["source_dirty"] is False
    (tmp_path / "private.db").write_text("private")
    assert build_info.create_manifest(tmp_path)["source_sha256"] == first["source_sha256"]
    (tmp_path / "main.py").write_text("print('b')")
    second = build_info.create_manifest(tmp_path)
    assert second["source_dirty"] is True
    assert second["source_sha256"] != first["source_sha256"]


def test_frozen_manifest_is_loaded_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info.sys, "frozen", True, raising=False)
    monkeypatch.setattr(build_info.sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "build_info.json").write_text(json.dumps({"schema_version": 1, "build_id": "test"}))
    assert build_info.load_build_info()["build_id"] == "test"
    (tmp_path / "build_info.json").write_text("invalid")
    assert "损坏" in build_info.load_build_info()["build_id"]
