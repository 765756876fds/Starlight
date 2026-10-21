"""LRC 歌词解析与本地 .lrc 读取。

标准 LRC：[mm:ss.xx] / [mm:ss.xxx] 时间轴 + 歌词文本；元标签行
（[ti:]/[ar:]/[al:]/[by:]/[offset:]）按无时间轴行过滤；空行过滤；
同一行多个时间戳（[00:01.00][00:02.00]词）展开为多条。

- parse_lrc(text)：纯解析（在线歌词 / .lrc 内容共用）
- find_local_lrc(audio_path)：查同名 .lrc（xxx.mp3 → xxx.lrc）
- load_lyrics_from_file(lrc_path)：读文件（UTF-8 → GBK 回退）+ 解析
"""
import os
import re

# [mm:ss.xx] / [mm:ss.xxx] / [m:ss.x]
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# 元标签（时间戳正则匹配不到 → 自然过滤；这里显式列出便于调试/扩展）
_META_TAGS = ("ti", "ar", "al", "by", "offset", "length", "re", "ve", "au")


def _ts_to_seconds(mm: str, ss: str, frac: str | None) -> float:
    seconds = int(mm) * 60 + int(ss)
    if frac:
        # xx（厘秒）/ xxx（毫秒）统一按小数位换算
        seconds += int(frac) / (10 ** len(frac))
    return seconds


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """解析 LRC 文本 → [(time_sec, line_text), ...]，按时间升序。

    - 无时间轴的行（元标签/空行）忽略
    - 一行多个时间戳 → 每个时间戳一条（同文本）
    - offset 标签（[offset:+500] 毫秒）应用到所有时间戳
    """
    lines: list[tuple[float, str]] = []
    offset_ms = 0
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # [offset:±N] 元标签：毫秒偏移，应用到后续所有行
        m = re.match(r"^\[offset:([+-]?\d+)\]$", line, re.IGNORECASE)
        if m:
            offset_ms = int(m.group(1))
            continue
        matches = _TIMESTAMP_RE.findall(line)
        if not matches:
            continue  # 元标签行 / 无时间轴行 → 过滤
        body = _TIMESTAMP_RE.sub("", line).strip()
        if not body:
            continue  # 只有时间戳没有文本
        for mm, ss, frac in matches:
            t = _ts_to_seconds(mm, ss, frac) + offset_ms / 1000.0
            lines.append((max(0.0, t), body))
    lines.sort(key=lambda x: x[0])
    return lines


def find_local_lrc(audio_path: str | None) -> str | None:
    """查同名 .lrc：xxx.mp3 → 同目录 xxx.lrc。不存在/路径非法返回 None。"""
    if not audio_path:
        return None
    base, _ext = os.path.splitext(audio_path)
    lrc_path = base + ".lrc"
    if os.path.isfile(lrc_path):
        return lrc_path
    return None


def load_lyrics_from_file(lrc_path: str) -> list[tuple[float, str]]:
    """读 .lrc 文件（UTF-8 优先，GBK 回退）并解析。读失败返回 []。"""
    try:
        text = _read_text_tolerant(lrc_path)
    except Exception:
        return []
    return parse_lrc(text)


def _read_text_tolerant(path: str) -> str:
    """UTF-8 strict → GBK 回退（常见中文 .lrc 用 GBK 保存）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        with open(path, "r", encoding="gbk", errors="replace") as f:
            return f.read()
