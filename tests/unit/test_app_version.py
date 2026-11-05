import subprocess
from pathlib import Path

import pytest

from app import version
from app.core import build_info


def test_source_version_comes_from_the_single_version_file():
    root = Path(__file__).resolve().parents[2]
    assert version.get_version() == (root / "VERSION").read_text(encoding="utf-8").strip()
    assert build_info.load_build_info()["app_version"] == version.get_version()
    assert "版本：" + version.get_version() in build_info.describe_build()


def test_frozen_version_reads_the_bundled_version_file(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.2.3.dev4\n", encoding="utf-8")
    monkeypatch.setattr(version.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert version.get_version() == "1.2.3.dev4"


@pytest.mark.parametrize("content", [None, "", "not a version", "1.2.3\nprivate data"])
def test_missing_or_invalid_version_is_unknown_not_a_fake_release(tmp_path, content):
    if content is not None:
        (tmp_path / "VERSION").write_text(content, encoding="utf-8")
    assert version.get_version(tmp_path) == "unknown"


def test_legacy_user_version_does_not_override_product_or_rewrite_settings(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "migrate_legacy_user_data", lambda: None)
    settings = tmp_path / "settings.yaml"
    content = "app:\n  version: 0.1.0\n  name: 保留名称\nmusic:\n  default_volume: 31\n"
    settings.write_text(content, encoding="utf-8")
    cfg = config.Config(str(settings))
    assert cfg.get("app.version") == version.get_version()
    assert cfg.get("music.default_volume") == 31 and cfg.get("app.name") == "保留名称"
    assert settings.read_text(encoding="utf-8") == content


def test_manifest_fingerprint_changes_with_the_version(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    git("add", "VERSION")
    git("commit", "-m", "test")
    first = build_info.create_manifest(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.4.dev1\n", encoding="utf-8")
    second = build_info.create_manifest(tmp_path)
    assert first["app_version"] == "1.2.3" and second["app_version"] == "1.2.4.dev1"
    assert second["source_dirty"] is True
    assert first["source_sha256"] != second["source_sha256"]


def test_packaging_and_both_installers_read_the_same_version():
    root = Path(__file__).resolve().parents[2]
    assert "(P('VERSION'), '.')" in (root / "scripts/build.spec").read_text(encoding="utf-8")
    include = (root / "scripts/version.iss").read_text(encoding="utf-8")
    assert '"..\\VERSION"' in include and "FileRead" in include and "FileClose" in include
    for name in ("setup_full.iss", "setup_lite.iss"):
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert '#include "version.iss"' in text
        assert "VersionInfoVersion={#MyAppNumericVersion}" in text
        assert '#define MyAppVersion "' not in text
