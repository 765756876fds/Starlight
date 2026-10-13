"""PHASE C 数据层小修测试。"""
from app.lol.data import AbilityCatalog, ChampionCatalog
from app.lol.cooldown.manager import CooldownManager
from tests.test_lol_cooldown_e2e import FakeScheduler


def test_964_abilities():
    import json
    d = json.load(open("data/lol_data/abilities.json", encoding="utf-8"))
    # 数据可经设置页「更新数据」按钮刷新（用户实机 964→1000）：
    # 断言下界而非硬编码，避免数据更新后测试过时。
    assert len(d) >= 964
    champs = {a["champion_id"] for a in d}
    assert 3 in champs, "加里奥 id=3 必须存在"
    assert len(champs) >= 241


def test_cooldown_by_level_present():
    import json
    d = json.load(open("data/lol_data/abilities.json", encoding="utf-8"))
    yasuo_w = next(a for a in d if a["champion_id"] == 157 and a["slot"] == "W")
    assert "cooldown_by_level" in yasuo_w
    assert yasuo_w["cooldown_by_level"] == [25.0, 23.0, 21.0, 19.0, 17.0, 15.0]


def test_get_cooldown_by_level():
    c = AbilityCatalog()
    # 亚索 W：1级 25, 5级 17
    assert c.get_cooldown_by_level(157, "W", 1) == 25.0
    assert c.get_cooldown_by_level(157, "W", 5) == 17.0
    # 越界 clamp
    assert c.get_cooldown_by_level(157, "W", 99) == 15.0
    assert c.get_cooldown_by_level(157, "W", 0) == 25.0


def test_cd_zero_returns_none():
    c = AbilityCatalog()
    # 艾希 Q = 0
    assert c.get_cooldown(22, "Q") is None
    # 艾希 W 正常
    assert c.get_cooldown(22, "W") == 18.0


def test_invalid_skill_event_on_cd_zero():
    # 直接验证：AbilityCatalog.get_cooldown(22,Q) is None
    assert AbilityCatalog().get_cooldown(22, "Q") is None
    # 验证 manager 代码路径：mock abilities 返回 None
    from app.lol.data import ChampionCatalog, SummonerSpellCatalog
    events, speeches = [], []
    sched = FakeScheduler()

    class ZeroCatalog(AbilityCatalog):
        def get_cooldown_by_level(self, cid, slot, level=1):
            # 生产路径 start_reminder 调 get_cooldown_by_level（不是 get_cooldown）
            return None

    mgr = CooldownManager(
        champions=ChampionCatalog(),
        abilities=ZeroCatalog(),
        spells=SummonerSpellCatalog(),
        on_event=lambda c, d: events.append((c, d)),
        speech_enqueue=lambda t, e: speeches.append((t, e)),
        remind_before=5.0,
        scheduler=sched,
    )
    r = mgr.start_reminder("亚索", "Q")
    assert r is None
    assert any(c == "cooldown.invalid_skill" for c, _ in events)
    assert any("没有固定冷却" in t for t, _ in speeches)


def test_loader_ranked_whitelist_excludes_custom():
    """loader 段位行/战绩：队列白名单 {400,420,430,440,460,490}。
    自定义 0 / 未知 880 / 人机 870 不再计入 wins/total/recent_matches。"""
    from app.lol.ongoing.loader import OngoingGameLoader
    from app.lol.ongoing.match_model import MatchHistoryResult, MatchRecord, AVAILABLE

    class FakeLcu:
        _connected = True
        _session = {"connected": True}

        def get(self, endpoint):
            if "/lol-summoner/v1/summoners/" in endpoint:
                return {"displayName": "Player", "summonerLevel": 30,
                        "summonerId": 1, "puuid": "x"}
            if "/lol-ranked/v1/ranked-stats/" in endpoint:
                return {"queues": [{"queueType": "RANKED_SOLO_5x5",
                                    "tier": "GOLD", "division": "II", "leaguePoints": 20}]}
            return None

    class FakeAcct:
        def recent(self, puuid, count=None):
            return MatchHistoryResult(state=AVAILABLE, records=[
                MatchRecord(game_id=1, game_creation=1, queue_id=400, champion_name="Yasuo",
                            win=True, kills=5, deaths=2, assists=8),
                MatchRecord(game_id=2, game_creation=2, queue_id=0, champion_name="Garen",
                            win=True, kills=3, deaths=1, assists=4),   # 自定义 → 排除
                MatchRecord(game_id=3, game_creation=3, queue_id=880, champion_name="Ahri",
                            win=True, kills=9, deaths=3, assists=5),   # 未知娱乐 → 排除
                MatchRecord(game_id=4, game_creation=4, queue_id=870, champion_name="Lux",
                            win=False, kills=1, deaths=6, assists=2),  # 人机 → 排除
            ])

    session = {
        "myTeam": [{"puuid": "p", "summonerId": 1, "cellId": 0, "assignedPosition": "MIDDLE"}],
        "theirTeam": [],
    }
    l = OngoingGameLoader(lcu_get=FakeLcu().get, account_history_service=FakeAcct(),
                          max_workers=1, rps=100)
    snaps = l.load_for_champ_select(session)
    assert len(snaps) == 1
    s = snaps[0]
    assert len(s.recent_matches) == 1          # 只留 queue_id=400
    assert s.recent_matches[0].game_creation == 1
    assert s.total_matches == 1                # 自定义 0 / 880 / 870 那 3 场不再计入
    assert s.win_rate == 1.0                   # 1 胜 0 负
