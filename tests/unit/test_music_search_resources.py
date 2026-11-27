import json
import sys
from pathlib import Path

import pytest

from app.music.online.content_choices import equivalent_versions, group_tracks, same_recording
from app.music.online.models import OnlineTrack
from app.music.online.search_resources import search_resources
from app.ui.panels.music_search_order import rank_manual_search
from app.ui.panels.music_search_rows import resource_note, search_rows


def track(source, title="星桥", artist="纸鸢乐队", album="夜航", duration=180, native="1", **raw):
    return OnlineTrack(source, native, title, artist, album, duration, extra={"_raw": raw})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows NLS provides script normalization")
def test_mixed_script_and_declared_search_aliases():
    exact = track("A", "發如雪", "周杰倫")
    irrelevant = track("B", "发如雪精选", "周杰伦")
    alias = track("C", "雨後", "名稱 A", aliases=["雨滴"], artistAliases=["别名 A"])
    assert rank_manual_search([irrelevant, exact], "《发 如 雪》·周杰伦")[0] is exact
    assert rank_manual_search([irrelevant, alias], "雨滴 别名 A")[0] is alias
    assert len(group_tracks([exact, track("D", "发如雪", "周杰伦")])) == 1
    assert not same_recording(exact, track("D", "发如雪", "周杰伦"))


@pytest.mark.parametrize("variant", ["翻唱", "现场版", "伴奏", "DJ版", "Remix", "重新录制"])
def test_explicit_variant_and_conflicting_external_identity_are_vetoes(variant):
    a = track("A", isrc="US-ABC-24-00001")
    b = track("B", isrc="USABC2400001", versionType=variant)
    c = track("C", isrc="USABC2400002")
    assert group_tracks([a, b, c]) == [[a], [b], [c]]
    assert list(equivalent_versions(a, [b, c])) == []


def test_strong_id_supports_display_with_context_but_never_weakens_replacement():
    a = track("A", isrc="USABC2400001")
    b = track("B", duration=182, album="新选集", isrc="USABC2400001")
    c = track("C", duration=184, album="新选集", isrc="USABC2400001")
    d = track("D", artist="其他人", isrc="USABC2400001")
    e = track("E", musicbrainzRecordingId="f8f3bc80-9e5d-450e-9f79-e5e180eb4011")
    f = track("F", musicbrainzRecordingId="a3d67cf1-8e12-4a8f-86ee-cca211e8c632")
    assert group_tracks([a, b, c, d]) == [[a, b], [c], [d]]
    assert list(equivalent_versions(a, [b, c, d])) == []
    assert not same_recording(e, f)
    anchor = track("G")
    assert len(group_tracks([e, f, anchor])) == 3
    assert len(group_tracks([anchor, e, f])) == 3
    assert list(equivalent_versions(anchor, [e, f])) == []


def test_resource_quality_is_declaration_and_fields_do_not_leak_between_renditions():
    song = track("A", qualities={
        "high": {"size": "3.5 MB", "format": "mp3", "bitrate_kbps": 320, "verified": True},
        "super": {"sizeBytes": 15_200_000, "ext": "flac", "sampleRate": 96000, "bitDepth": 24},
        "hires": {"size": "128k"},
    })
    high, super_, hires = search_resources(song)
    assert high.size_bytes is None and high.bitrate_bps == 320_000
    assert high.evidence == "source_declared" and high.sample_rate_hz is None and high.bit_depth is None
    assert (super_.size_bytes, super_.format, super_.sample_rate_hz, super_.bit_depth) == (15_200_000, "FLAC", 96_000, 24)
    assert hires.size_bytes is None and hires.format is None
    assert search_resources(track("B", format="flac", size=True))[0].declared_quality is None


def test_size_and_heat_orders_are_independent_and_preserve_playback_target():
    a = track("A", title="星桥 A", playCount=12)
    late = track("B", title="星桥 A", qualities={"super": {"size": "8 MiB"}})
    b = track("A", title="星桥 B", native="2", playCount=60, sizeBytes=10000)
    c = track("B", title="星桥 C", native="2", playCount=9999)
    e = track("B", title="星桥 E", native="3", playCount=3)
    d = track("C", title="星桥 D", playCount="热门")
    groups = group_tracks([a, late, b, c, e, d])
    assert [r.track for r in search_rows(groups, "relevance")] == [a, b, c, e, d]
    sizes = search_rows(groups, "size")
    assert [r.track for r in sizes] == [a, b, c, e, d]
    assert sizes[0].size_resource.source == "B" and sizes[0].tracks == [a, late]
    assert resource_note(sizes[0], "size") == "最大 · 标注 8.0 MiB · 超高档"
    assert resource_note(sizes[-1], "size") == "大小未知"
    assert [r.track for r in search_rows(groups, "heat")] == [b, c, a, e, d]
