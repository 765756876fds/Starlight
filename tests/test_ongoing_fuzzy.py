"""Akari 式玩家模糊搜索（拼音首字母 + difflib）。"""
from app.lol.ongoing.fuzzy import py_initials, fuzzy_score, fuzzy_candidates


def test_py_initials_chinese():
    assert py_initials("空白书") == "kbs"
    assert py_initials("果子") == "gz"
    assert py_initials("KkS") == "kks"
    assert py_initials("abc 123") == "abc123"


def test_fuzzy_score_substring_and_pinyin():
    assert fuzzy_score("空白", "空白书") >= 80
    assert fuzzy_score("kbs", "空白书") >= 78
    assert fuzzy_score("kb", "空白书") >= 77
    assert fuzzy_score("书", "空白书") >= 80
    assert fuzzy_score("zzz", "空白书") < 20


def test_fuzzy_candidates_orders_by_score():
    names = ["空白书", "小白", "小李", "空青", "喵喵喵"]
    got = fuzzy_candidates("kb", names, top=3)
    assert got and got[0] == "空白书"


def test_fuzzy_candidates_pinyin_initial_prefix():
    names = ["空白书", "王小明", "陈小春"]
    got = fuzzy_candidates("kbs", names, top=3)
    assert got[0] == "空白书"


def test_fuzzy_candidates_threshold_filters():
    got = fuzzy_candidates("qqq", ["空白书", "王小明"], top=8)
    assert got == []
