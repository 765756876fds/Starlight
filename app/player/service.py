"""PlayerAnalysisService - 从 2999 + LCU 构建当前对局玩家快照"""
from typing import Optional, Dict, Any, List, Tuple

from .models import PlayerIdentity, GamePlayer, CurrentGamePlayers
from .repository import PlayerRepository


class PlayerAnalysisService:
    def __init__(self, repository: PlayerRepository, lcu=None):
        self.repo = repository
        self.lcu = lcu
        self._current: Optional[CurrentGamePlayers] = None
        self._last_signature: Optional[Tuple] = None

    def set_lcu(self, lcu):
        self.lcu = lcu

    def _signature(self, snapshot) -> Tuple:
        """玩家阵容签名：用于检测是否变化"""
        return tuple(
            (p.summoner_name, p.team, p.champion_id)
            for p in sorted(snapshot.players, key=lambda x: x.summoner_name)
        )

    def analyze_game(self, snapshot) -> Optional[CurrentGamePlayers]:
        """从 GameSnapshot 构建 CurrentGamePlayers

        阵营判断：以 self.team 为基准，同队=ally，异队=enemy。
        仅当玩家阵容变化时才重新解析 + 持久化。
        """
        if not snapshot or not snapshot.players:
            self._current = None
            self._last_signature = None
            return None

        sig = self._signature(snapshot)
        if sig == self._last_signature and self._current is not None:
            return self._current

        # 获取 self 的 puuid 从 LCU
        self_puuid = ""
        self_summoner_id = 0
        if self.lcu and self.lcu.connected:
            summoner = self.lcu.get_current_summoner()
            if summoner:
                self_puuid = summoner.get("puuid", "")
                self_summoner_id = summoner.get("summonerId", 0)

        # 先找 self 的 team
        self_team = ""
        for p in snapshot.players:
            if p.is_current_player:
                self_team = p.team
                break

        result = CurrentGamePlayers()
        for p in snapshot.players:
            identity = PlayerIdentity(
                puuid=self_puuid if p.is_current_player else "",
                summoner_id=self_summoner_id if p.is_current_player else 0,
                game_name=p.summoner_name,
            )
            gp = GamePlayer(
                identity=identity,
                summoner_name=p.summoner_name,
                team=p.team,
                champion_name=p.champion_name,
                champion_id=p.champion_id,
                position=p.position,
                is_self=p.is_current_player,
            )
            if p.is_current_player:
                result.self_player = gp
            elif self_team and p.team == self_team:
                result.allies.append(gp)
            elif self_team:
                result.enemies.append(gp)
            # 如果 self_team 未知，不分类（安全失败）

            if identity.puuid:
                self.repo.upsert(identity)

        self._current = result
        self._last_signature = sig
        return result

    @property
    def current(self) -> Optional[CurrentGamePlayers]:
        return self._current
