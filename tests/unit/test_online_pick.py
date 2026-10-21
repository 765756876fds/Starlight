"""在线选源：音质提取 / 归一化 / 首选选择 单测。"""
import pytest

pytestmark = pytest.mark.unit

from app.music.controller import (
    _kw_bonus, _normalize, _pick_best, _pick_candidates, _quality_score,
)
from app.music.online.models import OnlineTrack


def _trk(name, title, artist, raw):
    return OnlineTrack(source_name=name, track_id="t1", title=title,
                       artist=artist, extra={"_raw": raw})


class FakeMgr:
    """最小 manager 替身：get_latency 支持固定值或按源 dict。"""
    def __init__(self, latency=None):
        self._lat = latency

    def get_latency(self, name):
        if isinstance(self._lat, dict):
            return self._lat.get(name)
        return self._lat


# ---------- _quality_score（字段名来自真实搜索结果核对）----------

def test_quality_kg_super_is_hires():
    raw = {"qualities": {"low": {}, "standard": {}, "high": {}, "super": {}}}
    assert _quality_score("kg", raw) == 4


def test_quality_kw_high_is_high_quality():
    raw = {"qualities": {"low": {}, "standard": {}, "high": {}}}
    assert _quality_score("kw", raw) == 3


def test_quality_wy_standard_only():
    raw = {"qualities": {"low": {}, "standard": {}}}
    assert _quality_score("wy", raw) == 2


def test_quality_suyin_url_bitrate():
    assert _quality_score("溯音网易", {"url": "https://share.duanx.cn/url/wy/1/128k"}) == 1
    assert _quality_score("溯音网易", {"url": "https://share.duanx.cn/url/wy/1/320k"}) == 3


def test_quality_unknown_plugins_return_zero():
    # 开心汽水 / 酷我念心 raw 无音质字段（真实数据核对）
    assert _quality_score("开心汽水", {"id": "1", "title": "晴天"}) == 0
    assert _quality_score("酷我念心", {"id": "1", "title": "晴天"}) == 0


def test_quality_tx_compat_fields():
    assert _quality_score("tx", {"size_flac": 123}) == 3
    assert _quality_score("tx", {"size_320mp3": 123}) == 2


def test_quality_no_false_positive_on_sql_highlight():
    # 无 qualities 结构时，短词 sql/highlight 不误判
    raw = {"note": "sql highlight super_high 320kbps is not a real plugin schema"}
    assert _quality_score("wy", raw) == 0


# ---------- _normalize ----------

def test_normalize_case_punct_feat():
    assert _normalize("  JAY Chou (feat. Jay)  ") == "jaychou"
    assert _normalize("晴天(深情版)") == _normalize("晴天深情版")
    assert _normalize("Sunny Day") == "sunnyday"


# ---------- _pick_best ----------

def test_pick_best_quality_wins_in_same_group():
    tracks = [
        _trk("kw", "晴天", "周杰伦", {"qualities": {"low": {}, "standard": {}}}),          # 2
        _trk("kg", "晴天", "周杰伦", {"qualities": {"low": {}, "high": {}}}),              # 3
    ]
    assert _pick_best(tracks, FakeMgr()).source_name == "kg"


def test_pick_best_faster_wins_same_quality():
    tracks = [
        _trk("kw", "晴天", "周杰伦", {"qualities": {"low": {}, "high": {}}}),   # 3 慢
        _trk("kg", "晴天", "周杰伦", {"qualities": {"low": {}, "high": {}}}),   # 3 快
    ]
    mgr = FakeMgr({"kw": 0.9, "kg": 0.1})
    assert _pick_best(tracks, mgr).source_name == "kg"


def test_pick_best_kw_bonus_when_tied():
    tracks = [
        _trk("kg", "晴天", "周杰伦", {"qualities": {"low": {}, "high": {}}}),
        _trk("kw", "晴天", "周杰伦", {"qualities": {"low": {}, "high": {}}}),
    ]
    # 同音质同耗时（首轮无记录 → 99.0）→ 酷我优先
    assert _pick_best(tracks, FakeMgr(latency=None)).source_name == "kw"


def test_pick_best_does_not_steal_from_other_song():
    # 反例：搜"晴天"，别的歌手的 flac 不能抢周杰伦的 320
    tracks = [
        _trk("kw", "晴天", "周杰伦", {"qualities": {"low": {}, "standard": {}}}),   # 2，首条=首选组
        _trk("kg", "晴天", "晚风纯音", {"qualities": {"low": {}, "high": {}}}),     # 3，但不同歌
    ]
    picked = _pick_best(tracks, FakeMgr())
    assert picked.title == "晴天" and picked.artist == "周杰伦"   # 没跑偏到翻唱
    assert picked.source_name == "kw"


def test_pick_best_single_track():
    t = _trk("kw", "晴天", "周杰伦", None)
    assert _pick_best([t], FakeMgr()) is t


# ---------- _pick_candidates（failover 候选列表）----------

def test_pick_candidates_returns_sorted_list():
    tracks = [
        _trk("kw", "晴天", "周杰伦", {"qualities": {"low": {}, "standard": {}}}),   # 2 慢
        _trk("kg", "晴天", "周杰伦", {"qualities": {"low": {}, "high": {}}}),       # 3 快
    ]
    mgr = FakeMgr({"kw": 0.9, "kg": 0.1})
    cands = _pick_candidates(tracks, mgr)
    assert [c.source_name for c in cands] == ["kg", "kw"]   # 音质优先


def test_pick_candidates_excludes_other_song():
    # 别的歌手的 flac 不进候选（首选组 = 首条所在组）
    tracks = [
        _trk("kw", "晴天", "周杰伦", {"qualities": {"low": {}, "standard": {}}}),
        _trk("kg", "晴天", "晚风纯音", {"qualities": {"low": {}, "high": {}}}),
    ]
    cands = _pick_candidates(tracks, FakeMgr())
    assert [c.artist for c in cands] == ["周杰伦"]


def test_pick_candidates_empty():
    assert _pick_candidates([], FakeMgr()) == []


def test_pick_candidates_single():
    t = _trk("kw", "晴天", "周杰伦", None)
    assert _pick_candidates([t], FakeMgr()) == [t]


# ---------- _kw_bonus ----------

def test_kw_bonus():
    assert _kw_bonus("酷我音乐") == 0
    assert _kw_bonus("kw") == 0
    assert _kw_bonus("kg") == 1
    assert _kw_bonus("开心汽水") == 1
