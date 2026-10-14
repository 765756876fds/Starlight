"""MatchSummary Optional 化回归测试。

52c3d23 候选 A 语义：缺失场次（win/kills/deaths/assists 为 None）必须原样保留，
不能归一为 False/0——否则回退分支无法区分"真负"与"未知"。
"""
from app.lol.ongoing.loader import OngoingGameLoader
from app.lol.ongoing.match_model import MatchRecord


def _rec(win=None, kills=None, deaths=None, assists=None):
    return MatchRecord(
        champion_id=1,
        champion_name="Thresh",
        win=win,
        kills=kills,
        deaths=deaths,
        assists=assists,
        game_duration=1500,
        game_creation=1700000000000,
    )


def test_win_none_stays_none():
    s = OngoingGameLoader._to_summary(_rec(win=None))
    assert s.win is None


def test_kda_missing_stays_none():
    s = OngoingGameLoader._to_summary(_rec(kills=None, deaths=None, assists=None))
    assert s.kills is None
    assert s.deaths is None
    assert s.assists is None


def test_win_true_false_preserved():
    s_win = OngoingGameLoader._to_summary(_rec(win=True, kills=3, deaths=1, assists=2))
    assert s_win.win is True
    assert s_win.kills == 3
    s_loss = OngoingGameLoader._to_summary(_rec(win=False, kills=1, deaths=5, assists=0))
    assert s_loss.win is False
    assert s_loss.deaths == 5
