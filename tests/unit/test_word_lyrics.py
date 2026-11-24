"""Timing fidelity at the boundary between source lyrics, the reader and the stage."""
import pytest

from app.music.lyrics import parse_document, parse_lrc, plugin_lyric_text


def test_lx_times_are_relative_with_gaps_and_overlaps():
    doc = parse_document("[offset:250]\n[00:02.00]<150,83>春<450,720>天<850,200>到了")
    assert doc.lines == [(2.25, "春天到了")]
    words = doc.words[0]
    assert [doc.lines[0][1][w.first:w.last] for w in words] == ["春", "天", "到了"]
    assert [w.start for w in words] == pytest.approx([2.4, 2.7, 3.1])
    assert [w.end for w in words] == pytest.approx([2.483, 3.42, 3.3])


def test_enhanced_lrc_uses_absolute_times_and_preserves_unknown_tail():
    doc = parse_document("[00:01.10]<00:01.3>🌙 <00:02.22>blue<00:02.75>\n"
                         "[00:04.00]<00:04.130>天<00:05.72>光")
    assert doc.lines == [(1.1, "🌙 blue"), (4., "天光")]
    a, b = doc.words[0]
    assert (a.first, a.last, a.start, a.end) == (0, 2, 1.3, 2.22)
    assert (b.first, b.last, b.start, b.end) == (2, 6, 2.22, 2.75)
    assert doc.words[1][-1].end is None


@pytest.mark.parametrize("body", ["<00:02.02>有<00:01.33>光", "<0,60>有<00:02>光"])
def test_malformed_order_or_mixed_time_bases_fall_back_to_a_line(body):
    doc = parse_document("[00:01.00]" + body)
    assert doc.lines == [(1., "有光")] and not doc.words


def test_plain_reading_and_line_only_have_no_word_cues():
    doc = parse_document("[ti:晓]\n夜里有光\nWe still rise")
    assert doc.plain == ["夜里有光", "We still rise"]
    assert not doc.words and not doc.lines
    assert parse_lrc("夜里有光") == []
    timed = parse_document("[00:01.00][00:05.00]夜里有光")
    assert timed.lines == [(1., "夜里有光"), (5., "夜里有光")]
    assert not timed.words and not timed.plain


def test_partial_plugin_track_preserves_metadata_other_verses_and_exact_text():
    ordinary = "[ti:星]\n[00:01.00]起 飞\n[00:03]第二段\n[00:05]保留"
    extra = "[00:01]<0,100>起<400,190>飞\n[00:03]<50,300>第<488,90>二段"
    merged = plugin_lyric_text(ordinary, extra)
    assert merged.startswith("[ti:星]\n")
    doc = parse_document(merged)
    assert doc.lines == parse_lrc(ordinary)
    assert set(doc.words) == {1}
    assert doc.words[1][-1].start == pytest.approx(3.488)
    assert plugin_lyric_text(ordinary, "invalid") == ordinary
    assert plugin_lyric_text("", extra) == extra


@pytest.mark.integration
def test_isolated_musicfree_and_lx_bridge_preserve_real_word_fields(tmp_path):
    from app.music.online.models import OnlineTrack
    from app.music.online.sources.js_source import JSMusicSource
    mf, lx = tmp_path/"mf", tmp_path/"lx"
    mf.mkdir()
    lx.mkdir()
    (mf/"timed.js").write_text(
        "module.exports={platform:'MF-fixture',getLyric:async item=>"
        "({rawLrc:'[00:01]<00:01.10>星<00:01.60>光<00:02.30>'})};", "utf-8")
    (lx/"LX-fixture.js").write_text(
        "lx.on('request',async ({source,action,info})=>{"
        "if(action!=='lyric'||source!=='local'||info.musicInfo.id!=='owned')throw Error('contract');"
        "return {lyric:'[00:01]星光',lxlyric:'[00:01]<100,200>星<600,700>光'};});", "utf-8")
    src = JSMusicSource(str(mf), str(lx))
    try:
        for name in ("LX-fixture", "MF-fixture"):
            track = OnlineTrack(name, "owned", "星光", "作者", "", 5, None,
                                {"_raw": {"id": "owned", "source": "local"}})
            result = parse_document(src.get_lyric(track))
            assert result.lines == [(1., "星光")]
            assert [w.start for w in result.words[0]] == pytest.approx([1.1, 1.6])
            assert result.words[0][1].end == pytest.approx(2.3)
    finally:
        src.close()
