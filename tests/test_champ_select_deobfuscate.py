"""
CS-DEOBFUSCATE-01: ChampSelect 阶段 HIDDEN 反解集成测试

场景：
① 正常反解：cs myTeam 4 HIDDEN（有 obfuscatedPuuid）+ 自己 UNHIDDEN → session 构建
   （session_created my_team=5 且 5 个 puuid 全非空；theirTeam 5 个 HIDDEN 无 obfuscated → 占位）
② feature flag 关闭（deobfuscate_enabled=False）→ 不构建 session
③ 反解失败（obfuscatedPuuid 非法）→ 无损降级：session 仍构建但只有自己有 puuid
"""
from unittest.mock import MagicMock

from app.lol.ongoing.service import OngoingGameService

SELF_PUUID = "dac5d02a-68ed-5596-b089-d193bc2944cf"
OBFUSCATED = [
    "31f5f18b-e380-0f4b-2470-4ddfe6821ac9",
    "0d6f0f2e-9854-02ba-30d5-2bd327858da2",
    "f7ca414f-2cf2-03c6-0cc2-79ddf3479d79",
    "5aa5fec1-5ce3-0cb8-0354-afcd2bd61e4b",
]


def _make_cs_session(obfuscated_list):
    """构造与 identity_diag_20261001_105951_cs.json 同构的 cs session"""
    my_team = []
    for i, ob in enumerate(obfuscated_list):
        my_team.append({
            "cellId": 5 + i, "teamId": 2, "summonerId": 0, "puuid": "",
            "riotIdGameName": "", "riotIdTagLine": "",
            "nameVisibilityType": "HIDDEN", "obfuscatedPuuid": ob,
        })
    my_team.append({
        "cellId": 9, "teamId": 2, "summonerId": 18026282441,
        "puuid": SELF_PUUID, "riotIdGameName": "", "riotIdTagLine": "",
        "nameVisibilityType": "UNHIDDEN", "obfuscatedPuuid": "",
    })
    their_team = []
    for i in range(5):
        their_team.append({
            "cellId": i, "teamId": 1, "summonerId": 0, "puuid": "",
            "riotIdGameName": "", "riotIdTagLine": "",
            "nameVisibilityType": "HIDDEN", "obfuscatedPuuid": "",
        })
    return {"myTeam": my_team, "theirTeam": their_team, "gameId": 0}


class _FakeLCU:
    """可控 lcu 桩：_connected/_session 通过 _tick 熔断检查；get 按 endpoint 返回"""
    _connected = True
    _session = {"ready": True}
    base_url = ""
    _auth_token = ""

    def __init__(self, responses):
        self._responses = responses

    def get(self, endpoint):
        return self._responses.get(endpoint)

    def request_raw(self, method, endpoint, data=None):
        return 200, {}


def _make_service(cs_session, enabled=True):
    events = []
    lcu = _FakeLCU({
        "/lol-gameflow/v1/gameflow-phase": "ChampSelect",
        "/lol-champ-select/v1/session": cs_session,
        "/lol-summoner/v1/current-summoner": {"puuid": SELF_PUUID},
        "/lol-gameflow/v1/session": {},
    })
    svc = OngoingGameService(
        lcu=lcu,
        on_event=lambda code, data: events.append((code, data)),
        deobfuscate_enabled=enabled,
    )
    # loader 打桩：不真查 SGP（history 链路由 test_loader_* 单独覆盖）
    svc._loader = MagicMock()
    svc._loader.load_for_champ_select.return_value = []
    return svc, events


def _event(events, code):
    for c, d in events:
        if c == code:
            return d
    return None


class TestChampSelectDeobfuscate:
    def test_normal_deobfuscate_builds_session(self):
        """① 正常反解：session_created my_team=5 且 5 个 puuid 全非空"""
        svc, events = _make_service(_make_cs_session(OBFUSCATED))
        svc._tick()

        created = _event(events, "ongoing.session_created")
        assert created is not None, f"缺 session_created，事件={[c for c, _ in events]}"
        assert created["my_team"] == 5
        assert created["their_team"] == 5
        assert created["puuid"] == 5  # 自己 + 4 个反解

        src = _event(events, "ongoing.session_source")
        assert src == {"source": "champ_select_deobfuscated", "players": 10, "resolved": 5}

        assert svc._session is not None
        assert [p.puuid for p in svc._session.my_team] == [
            "b0858722-17d1-5fd0-b1e8-25cc28130d2e",
            "8c1f7987-6c05-5221-a54d-43c0e9149a45",
            "76ba37e6-d8a3-535d-995a-11ce3dd68a9e",
            "dbd58868-a8b2-5c23-96cc-c7dee54709ac",
            SELF_PUUID,
        ]
        # theirTeam 5 个 HIDDEN 无 obfuscated → 占位（puuid 空，intel 链路跳过）
        assert all(p.puuid == "" for p in svc._session.their_team)

    def test_flag_disabled_no_session(self):
        """② flag 关闭：不构建 session，不发 session_created"""
        svc, events = _make_service(_make_cs_session(OBFUSCATED), enabled=False)
        svc._tick()

        assert _event(events, "ongoing.session_created") is None
        assert svc._session is None

    def test_deobfuscate_failure_degrades_gracefully(self):
        """③ 反解失败（obfuscatedPuuid 非法）→ session 仍构建，只有自己有 puuid"""
        bad = ["not-a-uuid"] * 4
        svc, events = _make_service(_make_cs_session(bad))
        svc._tick()

        created = _event(events, "ongoing.session_created")
        assert created is not None
        assert created["my_team"] == 5
        assert created["puuid"] == 1  # 仅自己

        src = _event(events, "ongoing.session_source")
        assert src == {"source": "champ_select_deobfuscated", "players": 10, "resolved": 1}
        assert len(svc._session.my_team) == 5
        assert sum(1 for p in svc._session.my_team if p.puuid) == 1
