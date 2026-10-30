from types import SimpleNamespace

from app.core.self_check import collect_report, main


def test_success_requires_shazam_exports():
    report = collect_report(lambda name: SimpleNamespace(Shazam=object, Serialize=object))
    assert report["ok"]
    assert len(report["checks"]) == 7
    failed = collect_report(lambda name: SimpleNamespace())
    assert not failed["ok"]
    assert failed["checks"][-1]["error_type"] == "ImportError"


def test_nested_dependency_failure_is_reported_without_private_exception_text():
    def load(name):
        if name == "shazamio":
            raise ModuleNotFoundError("secret/path/token", name="pydantic")
        return SimpleNamespace()
    report = collect_report(load)
    assert not report["ok"]
    assert report["checks"][-1]["missing_module"] == "pydantic"
    assert "secret" not in str(report)


def test_report_never_overwrites_existing_file(tmp_path, monkeypatch):
    import pytest
    import app.core.self_check as check
    target = tmp_path / "report.json"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(check, "collect_report", lambda: {"ok": True})
    with pytest.raises(FileExistsError):
        main(["--report", str(target)])
    assert target.read_text(encoding="utf-8") == "original"
