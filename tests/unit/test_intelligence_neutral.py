"""候选 A 中性剔除语义测试：win 缺失不计胜负、KDA 缺失不参与平均、窗口同源。

覆盖：win_rate 分母 = wins+losses / average_kda 只算完整场次 /
unknown_count 只数 win=None / streak 遇 None 保持 break /
games 含数据不全场次 / 10 场窗口同步语义。
"""
import pytest

pytestmark = pytest.mark.unit

from app.lol.ongoing.intelligence import PlayerIntelligenceService
from app.lol.ongoing.match_model import MatchRecord


def _svc():
    return PlayerIntelligenceService()


def _rec(seq, queue_id=420, win=True, kills=5, deaths=2, assists=8):
    return MatchRecord(
        game_id=seq,
        game_creation=seq,
        game_duration=1800,
        queue_id=queue_id,
        champion_id=1,
        champion_name="Yasuo",
        win=win,
        kills=kills,
        deaths=deaths,
        assists=assists,
        team_position="MIDDLE",
        source="sgp",
    )


def _analyze(matches):
    return _svc().analyze(puuid="p1", matches=matches)


def test_win_rate_denominator_is_decided():
    """win=None 不计入分母：win_rate = wins/(wins+losses)。"""
    r = _analyze([
        _rec(1, win=True),   # 胜
        _rec(2, win=False),  # 负
        _rec(3, win=None),   # 未知：不计
        _rec(4, win=True),   # 胜
    ])
    assert r.games == 4
    assert r.wins == 2 and r.losses == 1
    assert abs(r.win_rate - 2 / 3) < 0.001  # 2/(2+1)，不是 2/4


def test_kda_missing_not_averaged():
    """kills=None 场次不参与平均 KDA。"""
    r = _analyze([
        _rec(1, kills=1, deaths=1, assists=1),    # KDA (1+1)/1 = 2.0
        _rec(2, kills=None, deaths=1, assists=1),  # 缺失：剔除
        _rec(3, kills=3, deaths=1, assists=1),    # KDA (3+1)/1 = 4.0
    ])
    assert abs(r.average_kda - 3.0) < 0.001  # (2+4)/2，不是 (2+0+4)/3
    assert r.unknown_count == 0  # win 都有，KDA 缺失不计入 unknown_count


def test_unknown_count_counts_only_missing_win():
    """win=None 才计数；win 完整 + KDA 缺失不计数；wins+losses+unknown == games。"""
    r = _analyze([
        _rec(1, win=True, kills=None, deaths=1, assists=1),  # win 完整 + KDA 缺失
        _rec(2, win=None),                                    # win 缺失
        _rec(3, win=False),                                   # 负
    ])
    assert r.games == 3
    assert r.wins == 1 and r.losses == 1 and r.unknown_count == 1
    assert r.wins + r.losses + r.unknown_count == r.games


def test_unknown_count_zero_when_all_complete():
    """全完整场次 → unknown_count == 0。"""
    r = _analyze([_rec(i) for i in range(1, 6)])
    assert r.unknown_count == 0
    assert r.games == 5 and r.wins == 5


def test_streak_breaks_on_unknown_win():
    """win=None 中断形态（break 保持，不跳过继续）。"""
    # 首场 win=None → 不判形态（streak 保持 0）
    r = _analyze([
        _rec(1, win=True),
        _rec(2, win=True),
        _rec(3, win=None),   # 最新（game_creation 最大）
    ])
    assert r.streak == 0
    # 中间 win=None → 连胜中断
    r2 = _analyze([
        _rec(1, win=True),
        _rec(2, win=True),
        _rec(3, win=None),
        _rec(4, win=True),
        _rec(5, win=True),
    ])
    assert r2.streak == 2  # 最新 2 连胜后遇 None 中断


def test_games_counts_all_whitelist_incl_unknown():
    """games = 白名单全部场次（含 win=None）；raw_games 不变。"""
    r = _analyze([
        _rec(1, win=True),
        _rec(2, win=None),
        _rec(3, queue_id=450, win=True),  # 大乱斗：白名单外
    ])
    assert r.games == 2
    assert r.raw_games == 3


def test_games_10_counts_unknown_win_rate_10_uses_decided():
    """10 场里 2 场 win=None → games_10=10、win_rate_10 用 8 场算。"""
    matches = [_rec(i, win=(i % 2 == 1)) for i in range(1, 9)]  # 1-8：4胜4负
    matches += [_rec(9, win=None), _rec(10, win=None)]           # 9-10：win=None
    r = _analyze(matches)
    assert r.games_10 == 10
    decided = sum(1 for m in matches if m.win is not None)
    w = sum(1 for m in matches if m.win)
    assert decided == 8 and w == 4
    assert abs(r.win_rate_10 - w / decided) < 0.001


def test_win_rate_10_denominator_decided():
    """win_rate_10 分母 = 有结果场次（与 20 场窗口同语义）。"""
    r = _analyze([
        _rec(1, win=True),
        _rec(2, win=None),
        _rec(3, win=None),
    ])
    assert r.games_10 == 3
    assert r.win_rate_10 == 1.0  # 1/(1+0)
