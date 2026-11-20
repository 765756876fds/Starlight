from collections import Counter

import pytest

from app.music.online.models import OnlineTrack
from app.ui.panels.music_search_order import rank_manual_search


@pytest.fixture
def results():
    return [
        OnlineTrack("先到", "other", "雨后信箱", "许木"),
        OnlineTrack("先到", "similar", "星桥日记", "纸鸢乐队"),
        OnlineTrack("先到", "cover", "星桥（翻唱）", "许木"),
        OnlineTrack("先到", "backing", "星桥（伴奏）", "纸鸢乐队"),
        OnlineTrack("后到", "shared", "星桥", "远舟"),
        OnlineTrack("后到", "live", "星桥（Live）", "纸鸢乐队"),
        OnlineTrack("后到", "remix", "星桥 (Remix)", "纸鸢乐队"),
        OnlineTrack("后到", "note", "星桥【电影原声】", "纸鸢乐队"),
        OnlineTrack("现场源", "shared", "星桥", "纸鸢乐队"),
        OnlineTrack("单曲源", "single", "星桥", "纸鸢乐队"),
    ]


def test_song_name_promotes_exact_titles_and_keeps_every_version(results):
    ordered = rank_manual_search(results, "星桥")
    assert [t.track_id for t in ordered] == [
        "shared", "shared", "single", "cover", "backing", "live", "remix", "note", "similar", "other",
    ]
    assert Counter(map(id, ordered)) == Counter(map(id, results))
    assert [t.source_name for t in ordered[:3]] == ["后到", "现场源", "单曲源"]
    assert results[0].track_id == "other"


@pytest.mark.parametrize("keyword", ["星桥 纸鸢乐队", "纸鸢乐队 - 星桥", "《星桥》·纸鸢乐队", "星桥纸鸢乐队"])
def test_song_and_artist_promote_the_named_performer(results, keyword):
    ordered = rank_manual_search(results, keyword)
    assert [t.track_id for t in ordered[:6]] == ["shared", "single", "backing", "live", "remix", "note"]
    assert ordered[0].source_name == "现场源"
    assert ordered[-1] is results[4]


@pytest.mark.parametrize(("keyword", "track_id"), [
    ("星桥 LIVE 纸鸢乐队", "live"),
    ("星桥 remix", "remix"),
    ("星桥 翻唱 许木", "cover"),
    ("星桥（伴奏）", "backing"),
])
def test_explicit_version_is_not_replaced_by_bare_title(results, keyword, track_id):
    assert rank_manual_search(results, keyword)[0].track_id == track_id


@pytest.mark.parametrize("keyword", ["纸鸢乐队", "未知标题", "桥", "", "《》"])
def test_ambiguous_input_preserves_incoming_order(results, keyword):
    assert rank_manual_search(results, keyword) == results


def test_english_word_and_symbol_matching_is_conservative():
    someone = OnlineTrack("A", "1", "Someone", "")
    note = OnlineTrack("A", "2", "One (Live)", "")
    exact = OnlineTrack("B", "3", "ONE", "")
    cpp = OnlineTrack("B", "4", "C++", "")
    c = OnlineTrack("B", "5", "C", "")
    tracks = [someone, note, exact, c, cpp]
    assert rank_manual_search(tracks, "Ｏｎｅ") == [exact, note, someone, c, cpp]
    assert rank_manual_search(tracks, "C++") == [cpp, someone, note, exact, c]
