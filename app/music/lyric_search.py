"""本地歌词（.lrc）模糊搜索索引。

- build(songs)：遍历本地库歌曲，查同名 .lrc 并解析出歌词行，
  构建 [(song, norm_line), ...] 内存索引（无 .lrc / 解析失败 → 跳过该歌）。
- search(query)：规范化输入（去标点/空白）后对每行打分：
  包含关系（q in line）→ 1.0；否则 difflib.SequenceMatcher ratio；
  > 阈值（默认 0.62）的歌曲按歌去重、取最高分、降序返回。
- invalidate()：本地库扫描完成后调用，下次 search 前重建（懒加载）。

标点清理双侧对称：索引行与输入都做 _norm，中文输入带标点（如
"天青色，等烟雨"）也能命中"天青色等烟雨"。
"""
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from app.music.lyrics import find_local_lrc, load_lyrics_from_file

# 标点/空白/装饰符统一折叠（中文全角 + 英文半角）
_NORM_RE = re.compile(
    r"[\s,，。.!！?？、;；:：\"“”'‘’()（）\[\]【】{}<>《》~～·\-—_]+"
)

# 默认匹配阈值（SequenceMatcher ratio）
DEFAULT_THRESHOLD = 0.62
_SEARCH_LIMIT = 50


def _norm(text: Optional[str]) -> str:
    """去标点 + 去空白（用于输入与歌词行双侧匹配）。"""
    return _NORM_RE.sub("", text or "")


def _line_score(q: str, line: str) -> float:
    """行匹配分：包含 → 1.0；否则取 整体相似比 与 覆盖率 的较大值。

    覆盖率 = 最长连续匹配块长度 / len(q)（difflib 实现 partial_ratio 思路）：
    ratio 对长歌词行稀释严重（"吹着前奏" 对 22 字行仅 ~0.37），
    覆盖率捕捉"查询成段出现在行中"，CJK 歌词匹配更准。
    """
    if q in line or line in q:
        return 1.0
    sm = SequenceMatcher(None, q, line)
    blocks = sm.get_matching_blocks()
    best_block = max((b.size for b in blocks), default=0)
    return max(sm.ratio(), best_block / max(len(q), 1))


class LyricIndex:
    """本地歌词索引：构建 / 失效 / 模糊搜索。"""

    def __init__(self, db=None):
        self._db = db
        self._entries: List[Tuple[Dict, str]] = []  # [(song, norm_line), ...]
        self._built = False

    # ---- 构建 ----
    def build(self, songs: Optional[List[Dict]] = None) -> int:
        """全量重建索引，返回收录歌词行数。

        songs 缺省时用 db.get_all_songs()；无 .lrc / 读取/解析失败 → 跳过该歌。
        """
        if songs is None:
            if self._db is None:
                raise ValueError("LyricIndex 无 db，需显式传入 songs")
            songs = self._db.get_all_songs()
        entries: List[Tuple[Dict, str]] = []
        for s in songs or []:
            path = s.get("path")
            if not path:
                continue
            try:
                lrc = find_local_lrc(path)
                if not lrc:
                    continue
                lines = load_lyrics_from_file(lrc)  # [(time_sec, text), ...]
            except Exception:
                continue  # 单文件失败 → 跳过，不崩
            seen: set = set()
            for _t, text in lines:
                n = _norm(text)
                if n and n not in seen:
                    seen.add(n)
                    entries.append((s, n))
        self._entries = entries
        self._built = True
        return len(entries)

    def invalidate(self) -> None:
        """标记失效（本地库扫描完成后调用），下次 search 前重建。"""
        self._built = False
        self._entries = []

    def is_built(self) -> bool:
        return self._built

    def ensure_built(self, songs: Optional[List[Dict]] = None) -> int:
        """懒构建：未构建时全量建索引，返回行数。"""
        if not self._built:
            return self.build(songs)
        return len(self._entries)

    # ---- 搜索 ----
    def search(self, query: str, threshold: float = DEFAULT_THRESHOLD,
               limit: int = _SEARCH_LIMIT) -> List[Dict]:
        """按歌词片段模糊搜索 → 去重歌曲列表（按匹配度降序）。

        - 空查询 → []
        - 包含命中（规范化后 query in line）→ 1.0，否则 SequenceMatcher ratio
        - 每首歌只保留最高分；低于阈值不返回
        """
        q = _norm(query)
        if not q:
            return []
        best: Dict = {}  # key(path|id) -> (song, score)
        for song, line in self._entries:
            if q in line:
                score = 1.0
            else:
                score = _line_score(q, line)
            if score < threshold:
                continue
            key = song.get("path") or song.get("id")
            if key is None:
                continue
            cur = best.get(key)
            if cur is None or score > cur[1]:
                best[key] = (song, score)
        ranked = sorted(best.values(), key=lambda x: -x[1])
        return [s for s, _score in ranked[:limit]]
