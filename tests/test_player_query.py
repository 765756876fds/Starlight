"""PlayerQuery tests - only current game queries (history removed)"""
from app.voice.intent import RuleBasedIntent
from app.player.current_game_analysis import richest, self_kda, economy
from app.lol.snapshot import GameSnapshot, PlayerInfo


def _snapshot():
    snap = GameSnapshot()
    snap.current_player = PlayerInfo(champion_name="Karthus", team="ORDER",
                                      kills=5, deaths=0, assists=0, level=18, cs=0)
    snap.players = [
        snap.current_player,
        PlayerInfo(champion_name="Sivir", team="CHAOS", kills=0, deaths=1, assists=0, level=1),
        PlayerInfo(champion_name="Nocturne", team="CHAOS", kills=0, deaths=1, assists=0, level=1),
    ]
    snap.ally_team = [snap.current_player]
    snap.enemy_team = snap.players[1:]
    return snap


def test_current_self_kda():
    k = self_kda(_snapshot())
    assert k["champion"] == "Karthus"
    assert k["kills"] == 5
    print("OK: current_self_kda")


def test_current_richest():
    r = richest(_snapshot(), "enemy")
    assert r is not None
    assert r["champion"] == "Sivir"
    print("OK: current_richest")


def test_economy_diff():
    e = economy(_snapshot())
    assert e["state"] == "even"
    print("OK: economy_diff")


def test_intent_current_kda():
    intent = RuleBasedIntent.match("我现在几杀几死")
    assert intent.params["query_type"] == "current_self_kda"
    print("OK: intent current_kda")


def test_intent_richest():
    intent = RuleBasedIntent.match("对面谁最肥")
    assert intent.params["query_type"] == "current_richest"
    print("OK: intent current_richest")


def test_intent_economy():
    intent = RuleBasedIntent.match("经济差")
    assert intent.params["query_type"] == "economy_diff"
    print("OK: intent economy_diff")
