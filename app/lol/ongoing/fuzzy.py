# -*- coding: utf-8 -*-
"""Akari 式玩家模糊搜索：拼音首字母 + difflib 名称模糊。

- py_initials(text)：GB2312 区位 → 拼音首字母（字母/数字原样小写）。
  经典区间表：每个区间的汉字同音首字母相同（A-Y，Z 由 Y 区间覆盖）。
- fuzzy_score(query, name)：子串 > 拼音首字母前缀 > difflib 编辑相似度。
- fuzzy_candidates(query, names, ...)：排序取 top N（低于阈值剔除）。
"""
from __future__ import annotations

import difflib
from typing import Iterable, List

# GB2312 区位码 → 拼音首字母（A-Y；Z 无独立区间，归入 Y 尾部）
_GB2312_INITIAL_RANGES = [
    (0xB0A1, 0xB0C4, 'a'), (0xB0C5, 0xB2C0, 'b'), (0xB2C1, 0xB4ED, 'c'),
    (0xB4EE, 0xB6E9, 'd'), (0xB6EA, 0xB7A1, 'e'), (0xB7A2, 0xB8C0, 'f'),
    (0xB8C1, 0xB9FD, 'g'), (0xB9FE, 0xBBF6, 'h'), (0xBBF7, 0xBFA5, 'j'),
    (0xBFA6, 0xC0AB, 'k'), (0xC0AC, 0xC2E7, 'l'), (0xC2E8, 0xC4C2, 'm'),
    (0xC4C3, 0xC5B5, 'n'), (0xC5B6, 0xC5BD, 'o'), (0xC5BE, 0xC6D9, 'p'),
    (0xC6DA, 0xC8BA, 'q'), (0xC8BB, 0xC8F5, 'r'), (0xC8F6, 0xCBF9, 's'),
    (0xCBFA, 0xCDD9, 't'), (0xCDDA, 0xCEF3, 'w'), (0xCEF4, 0xD1B8, 'x'),
    (0xD1B9, 0xD4D0, 'y'), (0xD4D1, 0xD7F9, 'z'),
]


def _initial_of_char(ch: str) -> str:
    """单个字符 → 拼音首字母（小写）；ASCII 原样小写；非 GB2312 → 空串。"""
    if ch in " ":
        return ""
    try:
        b = ch.encode("gb2312")
    except (UnicodeEncodeError, UnicodeError):
        return ""
    if len(b) == 1:
        return ch.lower()
    code = (b[0] << 8) | b[1]
    for lo, hi, ini in _GB2312_INITIAL_RANGES:
        if lo <= code <= hi:
            return ini
    return ""


def py_initials(text: str) -> str:
    """「空白书」→ 'kbs'；「KkS」→ 'kks'（字母原样小写）。"""
    return "".join(_initial_of_char(c) for c in text)


def fuzzy_score(query: str, name: str) -> float:
    """0-100 匹配分。优先级：完全/子串 > 拼音首字母 > difflib 模糊。"""
    q = (query or "").strip().lower()
    n = (name or "").strip()
    if not q or not n:
        return 0.0
    nl = n.lower()
    if q == nl:
        return 100.0
    if q in nl:
        return 85.0
    if nl in q:                      # 输入包含完整名字（如补 #tag）
        return 60.0
    ini = py_initials(n)
    if ini and q == ini:
        return 90.0
    if ini and ini.startswith(q):
        return 78.0 - max(0, len(ini) - len(q)) * 0.25
    if ini and q in ini:             # 首字母子串（如 'kbs' 找 'kuangbai...'）
        return 65.0
    if len(q) >= 2:
        r = difflib.SequenceMatcher(None, q, nl).ratio()
        if r >= 0.5:
            return r * 50.0
    return 0.0


def fuzzy_candidates(query: str, names: Iterable[str],
                     top: int = 8, threshold: float = 20.0) -> List[str]:
    """对候选池按匹配分排序，返回 top N（低于阈值剔除）。"""
    scored = [(fuzzy_score(query, name), name) for name in names]
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [name for s, name in scored if s >= threshold][:top]
