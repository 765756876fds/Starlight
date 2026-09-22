"""当前对局面板：复用 Runtime 的 GameSnapshot，UI 不直接轮询 2999。

P.4.5 升级：加入玩家情报（Mastery Top3、当前英雄是否绝活）。
P.4.6 升级：加入最近20场胜率/KDA。
P.4.8 升级：加入常用位置。
"""
from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.lol.ongoing.champion_mastery import ChampionMasteryProvider, AVAILABLE
from app.lol.ongoing.intelligence_service import OngoingIntelligenceService
from app.lol.ongoing.sgp_server import SgpServerResolver
from app.lol.ongoing.sgp_provider import SgpMatchHistoryProvider
from app.lol.ongoing.intelligence import PlayerIntelligenceService
from app.lol.data.champion_catalog import ChampionCatalog


_POSITION_MAP = {
    "TOP": "上路", "JUNGLE": "打野", "MIDDLE": "中路",
    "BOTTOM": "下路", "UTILITY": "辅助",
}


def _fmt_game_time(t: float) -> str:
    try:
        s = int(float(t))
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "UNKNOWN"


class CurrentGamePanel(QWidget):
    """只读当前局状态。数据全部来自 snapshot_provider()，不自己访问 2999。"""

    def __init__(self, snapshot_provider=None, lcu_get=None, lcu_post=None, parent=None):
        super().__init__(parent)
        self._provider = snapshot_provider
        self._catalog = ChampionCatalog()

        # P.4.5/P.4.6：玩家情报服务
        self._intelligence = None
        if lcu_get:
            try:
                # Mastery
                mastery_provider = ChampionMasteryProvider(lcu_get=lcu_get)

                # SGP 战绩
                sgp_provider = None
                try:
                    resolver = SgpServerResolver(lcu_get)
                    _, sgp_base, _ = resolver.resolve()
                    if sgp_base:
                        def _get_token():
                            try:
                                r = lcu_get("/entitlements/v1/token")
                                return r.get("accessToken") if isinstance(r, dict) else None
                            except Exception:
                                return None
                        sgp_provider = SgpMatchHistoryProvider(
                            entitlements_token=_get_token,
                            sgp_base=lambda: sgp_base,
                        )
                except Exception as e:
                    print(f"[UI] SGP provider 初始化失败: {e}")

                # Intelligence Service
                self._intelligence = OngoingIntelligenceService(
                    mastery_provider=mastery_provider,
                    sgp_provider=sgp_provider,
                    intelligence_service=PlayerIntelligenceService(),
                )
                print(f"[UI] 玩家情报服务已初始化（SGP={'有' if sgp_provider else '无'}）")
            except Exception as e:
                print(f"[UI] 玩家情报服务初始化失败: {e}")

        root = QVBoxLayout(self)
        self.lbl = QLabel("未在对局中")
        self.lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.lbl)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def _format_player_line(self, player, is_self=False) -> str:
        """格式化单个玩家的显示行。"""
        prefix = "你: " if is_self else ""
        name = player.riot_game_name or player.summoner_name or "?"
        champ = player.champion_name or "?"
        k = player.kills
        d = player.deaths
        a = player.assists
        cs = player.cs

        lines = [f"{prefix}<b>{name}</b> {champ} {k}/{d}/{a} {cs}刀"]

        # P.4.6：最近20场
        if self._intelligence and player.puuid:
            intel = self._intelligence.get_player_intelligence(
                puuid=player.puuid,
                current_champion_id=player.champion_id,
            )

            # 最近20场
            if intel.recent_status == "AVAILABLE" and intel.recent_games > 0:
                wr_pct = intel.recent_win_rate * 100
                lines.append(
                    f"  近20场: {wr_pct:.1f}%胜率 · KDA {intel.recent_average_kda:.2f} "
                    f"(有效{intel.recent_games}场)"
                )
            elif intel.recent_status == "EMPTY":
                lines.append(f"  近20场: 无有效比赛")
            elif intel.recent_status == "UNAVAILABLE":
                lines.append(f"  近20场: 数据不可用")

            # P.4.8：常用位置
            if intel.habitual_positions:
                pos_names = [_POSITION_MAP.get(p, p) for p in intel.habitual_positions]
                lines.append(f"  常用位置: {' / '.join(pos_names)}")
            elif intel.recent_status == "AVAILABLE":
                lines.append(f"  常用位置: 数据不可用")

            # Mastery Top3
            if intel.mastery_status == AVAILABLE and intel.mastery_top3:
                top3_names = []
                for m in intel.mastery_top3:
                    c = self._catalog.get_by_id(m.champion_id)
                    name_zh = c.name_zh if c else f"#{m.champion_id}"
                    top3_names.append(f"{name_zh} Lv{m.champion_level}")
                lines.append(f"  绝活: {' · '.join(top3_names)}")
            elif intel.mastery_status == "UNAVAILABLE":
                lines.append(f"  绝活: 数据不可用")

            # 当前英雄是否绝活
            if intel.current_champion_is_top3 is True:
                lines.append(f"  <b style='color: gold'>本局绝活: 是</b>")
            elif intel.current_champion_is_top3 is False:
                lines.append(f"  本局绝活: 否")

        return "<br>".join(lines)

    def refresh(self):
        try:
            snap = self._provider() if self._provider else None
        except Exception:
            snap = None
        if snap is None:
            self.lbl.setText("未在对局中 / 暂无快照")
            # 游戏结束，清空情报缓存
            if self._intelligence:
                self._intelligence.clear()
            return

        cp = getattr(snap, "current_player", None)
        lines = [f"<b>当前对局</b>　时间 {_fmt_game_time(getattr(snap, 'game_time', 0))}"]

        if cp is None:
            lines.append("玩家: UNKNOWN")
        else:
            lines.append(self._format_player_line(cp, is_self=True))

        team = getattr(cp, "team", "") if cp else ""
        lines.append(f"队伍: {team or 'UNKNOWN'}")

        allies = getattr(snap, "ally_team", []) or []
        enemies = getattr(snap, "enemy_team", []) or []

        lines.append(f"<b>我方其他玩家：</b>")
        for p in allies:
            if p.is_current_player:
                continue
            lines.append(self._format_player_line(p))

        lines.append(f"<b>敌方玩家：</b>")
        for p in enemies:
            lines.append(self._format_player_line(p))

        self.lbl.setText("<br>".join(lines))
