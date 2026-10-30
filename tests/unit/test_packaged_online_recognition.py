from pathlib import Path


def test_build_spec_collects_shazamio_for_dynamic_online_recognition():
    spec = Path("scripts/build.spec").read_text(encoding="utf-8")

    assert "collect_all('shazamio')" in spec
    assert "shazam_hidden" in spec
    assert "shazam_datas" in spec
    assert "shazam_binaries" in spec
    assert "collect_all('pydantic')" in spec
    assert "collect_all('pydantic_core')" in spec
    assert "pydantic_hidden" in spec
    assert "pydantic_core_binaries" in spec
    assert "'pydantic'" not in spec.split("excludes=[", 1)[1].split("]", 1)[0]
