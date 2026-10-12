"""当前对局面板：双排卡片布局，左己方右敌方。"""
import threading
from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QScrollArea, QWidget

from app.lol.ongoing.champion_mastery import ChampionMasteryProvider, AVAILABLE
from app.lol.ongoing.intelligence_service import OngoingIntelligenceService
from app.lol.ongoing.sgp_server import SgpServerResolver
from app.lol.ongoing.sgp_provider import SgpMatchHistoryProvider
from app.lol.ongoing.intelligence import PlayerIntelligenceService
from app.lol.data.champion_catalog import ChampionCatalog
from app.ui.theme.colors import Colors


_POSITION_MAP = {
    "TOP": "上路", "JUNGLE": "打野", "MIDDLE": "中路",
    "BOTTOM": "下路", "UTILITY": "辅助",
}

_TIER_ZH = {
    "IRON": "黑铁", "BRONZE": "青铜", "SILVER": "白银", "GOLD": "黄金",
    "PLATINUM": "铂金", "EMERALD": "翡翠", "DIAMOND": "钻石", "MASTER": "大师",
    "GRANDMASTER": "宗师", "CHALLENGER": "王者",
}


def _fmt_game_time(t: float) -> str:
    try:
        s = int(float(t))
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "UNKNOWN"


class PlayerCard(QFrame):
    """单个玩家卡片组件"""
    def __init__(self, is_ally=True, show_skill=False, parent=None):
        super().__init__(parent)
        self.is_ally = is_ally
        border_color = Colors.ACCENT if is_ally else Colors.DANGER
        self.setStyleSheet(f"""
            PlayerCard {{
                border: 1px solid {border_color};
                border-radius: 6px;
                padding: 6px;
                margin: 2px;
                background: rgba(30, 30, 50, 0.8);
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(8, 6, 8, 6)

        self.row1 = QHBoxLayout()
        self.lbl_name = QLabel("玩家名")
        self.lbl_name.setStyleSheet("font-weight: bold; color: white;")
        self.lbl_streak = QLabel("")
        self.lbl_streak.setStyleSheet("font-size: 11px;")
        self.row1.addWidget(self.lbl_name, 1)
        self.row1.addWidget(self.lbl_streak)
        layout.addLayout(self.row1)

        self.lbl_line2 = QLabel("段位 · 位置 · 近20场")
        self.lbl_line2.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 12px;")
        layout.addWidget(self.lbl_line2)

        self.lbl_line3 = QLabel("KDA · 常用英雄")
        self.lbl_line3.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(self.lbl_line3)

        self.lbl_recent = QLabel("最近: -")
        self.lbl_recent.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(self.lbl_recent)

        # C 行（仅自己）：LCU 官方熟练度（全历史 mastery Top3）
        self.lbl_skill = None
        if show_skill:
            self.lbl_skill = QLabel("绝活: -")
            self.lbl_skill.setStyleSheet("color: #ffd700; font-size: 12px;")
            layout.addWidget(self.lbl_skill)

        self.lbl_current = QLabel("本局: -")
        self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 12px;")
        layout.addWidget(self.lbl_current)

    def update_data(self, name, level, rank, position, winloss, kda, champions,
                    is_mastery, streak_text="", champion_zh="",
                    recent_top="", skill_top=""):
        self.lbl_name.setText(f"{name}  Lv.{level}")
        if streak_text:
            if "胜" in streak_text:
                self.lbl_streak.setStyleSheet(f"color: {Colors.SUCCESS}; font-size: 11px; font-weight: bold;")
            else:
                self.lbl_streak.setStyleSheet(f"color: {Colors.DANGER}; font-size: 11px; font-weight: bold;")
            self.lbl_streak.setText(streak_text)
        else:
            self.lbl_streak.setText("")

        line2_parts = []
        if rank:
            line2_parts.append(rank)
        if position:
            line2_parts.append(position)
        if winloss:
            line2_parts.append(winloss)
        self.lbl_line2.setText(" · ".join(line2_parts) if line2_parts else "-")

        line3_parts = []
        if kda:
            line3_parts.append(kda)
        if champions:
            line3_parts.append(f"常用: {champions}")
        self.lbl_line3.setText(" · ".join(line3_parts) if line3_parts else "-")

        if recent_top:
            self.lbl_recent.setText(f"最近: {recent_top}")
        else:
            self.lbl_recent.setText("最近: -")

        # C 行：仅自己显示；无数据隐藏整行（不留"暂无数据"空行）
        if self.lbl_skill is not None:
            if skill_top:
                self.lbl_skill.setText(f"绝活: {skill_top}")
                self.lbl_skill.show()
            else:
                self.lbl_skill.setText("")
                self.lbl_skill.hide()

        if champion_zh:
            if is_mastery is True:
                self.lbl_current.setText(f"本局: {champion_zh}  ✅ 是绝活")
                self.lbl_current.setStyleSheet(f"color: {Colors.SUCCESS}; font-size: 12px;")
            elif is_mastery is False:
                self.lbl_current.setText(f"本局: {champion_zh}  ❌ 非绝活")
                self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;")
            else:
                self.lbl_current.setText(f"本局: {champion_zh}  绝活: 未知")
                self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: 12px;")
        else:
            self.lbl_current.setText("本局: -")


class CurrentGamePanel(QWidget):
    """双排卡片式当前局：左己方，右敌方。"""

    def __init__(self, snapshot_provider=None, lcu_get=None, lcu_post=None, ongoing_service=None, parent=None):
        super().__init__(parent)
        self._provider = snapshot_provider
        self._catalog = ChampionCatalog()

        self._intelligence = None
        self._loading_puuids: set = set()  # 情报查询进行中（防 1s 刷新重复触发）
        self._loading_lock = threading.Lock()  # _loading_puuids 跨线程读写保护
        self._ongoing = ongoing_service
        self._lcu_get = lcu_get
        if lcu_get:
            try:
                mastery_provider = ChampionMasteryProvider(lcu_get=lcu_get)
                self._intelligence = OngoingIntelligenceService(
                    mastery_provider=mastery_provider,
                    sgp_provider=None,
                    intelligence_service=PlayerIntelligenceService(),
                )
                self._intelligence.set_sgp_factory(self._try_create_sgp_provider)
                print(f"[UI] 玩家情报服务已初始化（SGP 动态懒加载）")
            except Exception as e:
                print(f"[UI] 玩家情报服务初始化失败: {e}")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 标题
        self.lbl_title = QLabel("未在对局中")
        self.lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: white;")
        root.addWidget(self.lbl_title)

        # 左右两列标题
        cols_header = QHBoxLayout()
        self.lbl_ally_title = QLabel("🟦 己方")
        self.lbl_ally_title.setStyleSheet("color: #4a9eff; font-weight: bold; font-size: 13px;")
        self.lbl_enemy_title = QLabel("🔴 敌方")
        self.lbl_enemy_title.setStyleSheet(f"color: {Colors.DANGER}; font-weight: bold; font-size: 13px;")
        cols_header.addWidget(self.lbl_ally_title)
        cols_header.addWidget(self.lbl_enemy_title)
        root.addLayout(cols_header)

        # 左右两列内容
        cols = QHBoxLayout()
        cols.setSpacing(8)

        # 左列：己方
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_content = QWidget()
        self.ally_layout = QVBoxLayout(left_content)
        self.ally_layout.setSpacing(6)
        left_scroll.setWidget(left_content)
        cols.addWidget(left_scroll, 1)

        # 右列：敌方
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_content = QWidget()
        self.enemy_layout = QVBoxLayout(right_content)
        self.enemy_layout.setSpacing(6)
        right_scroll.setWidget(right_content)
        cols.addWidget(right_scroll, 1)

        root.addLayout(cols)

        # 缓存卡片
        self.ally_cards = []
        self.enemy_cards = []

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def _try_create_sgp_provider(self):
        if not self._lcu_get:
            return None
        try:
            resolver = SgpServerResolver(self._lcu_get)
            _, sgp_base, _ = resolver.resolve()
            if not sgp_base:
                return None

            def _get_token():
                try:
                    r = self._lcu_get("/entitlements/v1/token")
                    return r.get("accessToken") if isinstance(r, dict) else None
                except Exception:
                    return None

            print(f"[UI] SGP provider 动态初始化成功: {sgp_base}")
            return SgpMatchHistoryProvider(
                entitlements_token=_get_token,
                sgp_base=lambda: sgp_base,
            )
        except Exception as e:
            print(f"[UI] SGP provider 动态初始化失败: {e}")
            return None

    def _get_or_create_card(self, team, idx, show_skill=False):
        cards = self.ally_cards if team == "ally" else self.enemy_cards
        layout = self.ally_layout if team == "ally" else self.enemy_layout
        while len(cards) <= idx:
            card = PlayerCard(is_ally=(team == "ally"), show_skill=show_skill)
            cards.append(card)
            layout.addWidget(card)
        return cards[idx]

    def _ensure_intel(self, puuid: str, champion_id: int):
        """懒触发玩家情报查询（后台线程，防重复）。"""
        if not puuid or self._intelligence is None:
            return
        with self._loading_lock:
            if puuid in self._loading_puuids:
                return
            if self._intelligence.get_cached(puuid) is not None:
                return
            self._loading_puuids.add(puuid)
        threading.Thread(
            target=self._fetch_intel, args=(puuid, champion_id), daemon=True
        ).start()

    def _fetch_intel(self, puuid: str, champion_id: int):
        try:
            self._intelligence.get_player_intelligence(puuid, champion_id)
        except Exception as e:
            print(f"[UI] 玩家情报查询失败 {puuid}: {e}")
        finally:
            with self._loading_lock:
                self._loading_puuids.discard(puuid)

    def _fill_player_card(self, card, player):
        """纯渲染：从 CurrentGamePlayer 三区读取（identity=gameflow / realtime=2999 / history=loader）。

        不再做任何身份匹配、缓存反查、历史/绝活查询或兜底。
        """
        snap_data = player.history  # PlayerSnapshot（loader 单链产物）或 None（尚未加载完成）
        is_self = bool(getattr(player, "is_self", False))

        name = player.display_name or "?"
        if is_self:
            name = f"你: {name}"

        # 本局英雄：identity.champion_id（gameflow 权威）→ 目录转中文
        champ = "?"
        if getattr(player, "champion_id", 0):
            c = self._catalog.get_by_id(player.champion_id) if self._catalog else None
            champ = c.name_zh if c and c.name_zh else (c.en_name if c else f"#{player.champion_id}")

        # 等级：优先历史召唤师等级（history），其次 2999 实时等级（realtime）
        level = "?"
        if snap_data is not None and getattr(snap_data, "summoner_level", 0):
            level = snap_data.summoner_level
        elif getattr(player, "live_level", 0):
            level = player.live_level

        # 位置：2999 实时位置优先（进局后），其次 gameflow assigned_position
        pos_raw = (getattr(player, "live_position", None) or getattr(player, "assigned_position", None) or "")
        pos_zh = ""
        if pos_raw and str(pos_raw).lower() not in ("none", "unselected", "unknown", "fill"):
            pos_map = {"top": "上路", "jungle": "打野", "mid": "中路", "bottom": "下路", "bot": "下路", "utility": "辅助"}
            pos_zh = pos_map.get(str(pos_raw).lower(), str(pos_raw))

        # 实时 KDA（realtime，2999）
        k = getattr(player, "live_kills", 0) or 0
        d = getattr(player, "live_deaths", 0) or 0
        a = getattr(player, "live_assists", 0) or 0

        winloss_text = ""
        hist_ready = (snap_data is not None
                      and getattr(snap_data, "data_state", None) == "AVAILABLE"
                      and getattr(snap_data, "recent_matches", None))
        if snap_data is not None and not hist_ready and getattr(snap_data, "data_state", None) != "AVAILABLE":
            if getattr(snap_data, "data_state", None) == "NO_DATA":
                # 请求成功但全是训练/娱乐模式（人机等）：确定的无匹配/排位战绩，不转圈
                kda_text = "无匹配/排位战绩"
            else:
                # 请求失败：自动重试中，显示获取中而不是误导性的0/0/0
                kda_text = "战绩获取中…（自动重试）"
        elif hist_ready:
            wins = sum(1 for m in snap_data.recent_matches if m.win)
            total = len(snap_data.recent_matches)
            kda_text = f"近{total}场 {wins}胜{total-wins}负 KDA {snap_data.kda_ratio:.2f}"
            winloss_text = f"{wins}胜{total-wins}负"
        elif snap_data is None:
            # history 尚未加载完成：实时 KDA 过渡（身份/玩家存在性不受影响）
            kda_text = f"战绩加载中… 实时 {k}/{d}/{a}"
        else:
            kda_text = f"{k}/{d}/{a}"
            if d > 0:
                kda_text += f" (KDA {(k + a) / d:.2f})"
            else:
                kda_text += " (KDA --)"

        is_mastery = None
        rank_text = ""
        champions_text = ""
        streak_text = ""

        # 段位（历史）：单双排 + 灵活组排
        if snap_data is not None and getattr(snap_data, "ranked_tier", None):
            tier = snap_data.ranked_tier
            rank_text = _TIER_ZH.get(tier.upper(), tier)
            if getattr(snap_data, "ranked_division", None):
                rank_text += f" {snap_data.ranked_division}"
            if getattr(snap_data, "ranked_lp", 0):
                rank_text += f" {snap_data.ranked_lp}胜点"
            flex_tier = getattr(snap_data, "ranked_flex_tier", None)
            if flex_tier:
                ft = _TIER_ZH.get(flex_tier.upper(), flex_tier)
                if getattr(snap_data, "ranked_flex_division", None):
                    ft += f" {snap_data.ranked_flex_division}"
                if getattr(snap_data, "ranked_flex_lp", 0):
                    ft += f" {snap_data.ranked_flex_lp}胜点"
                rank_text += f" · {ft}"

        # B 行（最近状态，所有人）+ C 行（LCU mastery，仅自己）：懒触发 + 缓存读
        intel_entry = None
        recent_top = ""
        skill_top = ""
        puuid = getattr(player, "puuid", "") or ""
        if puuid and self._intelligence is not None:
            self._ensure_intel(puuid, getattr(player, "champion_id", 0) or 0)
            intel_entry = self._intelligence.get_cached(puuid)
            if intel_entry is None:
                with self._loading_lock:
                    loading_now = puuid in self._loading_puuids
                recent_top = "获取中…" if loading_now else "-"
            elif intel_entry.recent_status == "AVAILABLE":
                stats = [c for c in (intel_entry.champion_stats or []) if c.games >= 5][:3]
                if stats:
                    recent_top = " · ".join(
                        f"{c.champion_name} {c.games}场/{int(c.win_rate * 100)}%"
                        for c in stats
                    )
                else:
                    recent_top = "样本不足"
            else:
                recent_top = "-"

            if (is_self and intel_entry is not None
                    and intel_entry.mastery_status == "AVAILABLE"
                    and intel_entry.mastery_top3):
                parts = []
                for m in intel_entry.mastery_top3[:3]:
                    c = self._catalog.get_by_id(m.champion_id) if self._catalog else None
                    zh = c.name_zh if c and c.name_zh else f"#{m.champion_id}"
                    pts = f"{m.champion_points // 1000}k" if m.champion_points >= 1000 else str(m.champion_points)
                    parts.append(f"{zh} Lv{m.champion_level} {pts}")
                skill_top = " · ".join(parts)

        # 本局是否使用 loader 常用英雄（is_mastery 用于 lbl_current，保留旧逻辑）
        if snap_data is not None and getattr(snap_data, "top_champions", None):
            top3_cids = []
            for cname in snap_data.top_champions[:3]:
                c = self._catalog.get_by_name(cname) if self._catalog else None
                top3_cids.append(c.cid if c else None)
            if getattr(player, "champion_id", 0):
                is_mastery = player.champion_id in top3_cids

        # 连胜/连败
        if snap_data is not None:
            st = getattr(snap_data, "win_streak", 0) or 0
            if st > 0:
                streak_text = f"{st}连胜"
            elif st < 0:
                streak_text = f"{-st}连败"

        card.update_data(
            name=name,
            level=level,
            rank=rank_text,
            position=pos_zh,
            winloss=winloss_text,
            kda=kda_text,
            champions=champions_text,
            is_mastery=is_mastery,
            streak_text=streak_text,
            champion_zh=champ,
            recent_top=recent_top,
            skill_top=skill_top,
        )

    def refresh(self):
        # 玩家集合唯一来源 = CurrentGameSession（gameflow 构建）；
        # 2999 只提供实时字段与游戏时间（渲染），不再决定"当前有哪些玩家"
        session = None
        if self._ongoing is not None:
            try:
                session = self._ongoing.get_session()
            except Exception:
                session = None

        game_time = 0
        if self._provider:
            try:
                snap = self._provider()
                if snap is not None:
                    game_time = getattr(snap, "game_time", 0) or 0
            except Exception:
                pass

        if session is None or not session.has_players:
            self.lbl_title.setText("未在对局中 / 选人中")
            for card in self.ally_cards + self.enemy_cards:
                card.hide()
            return

        n = len(session.players)
        self.lbl_title.setText(f"当前对局  时间 {_fmt_game_time(game_time)}  玩家 {n}")

        for i, p in enumerate(session.my_team):
            card = self._get_or_create_card("ally", i, show_skill=bool(getattr(p, "is_self", False)))
            card.show()
            self._fill_player_card(card, p)
        for i in range(len(session.my_team), len(self.ally_cards)):
            self.ally_cards[i].hide()

        for i, p in enumerate(session.their_team):
            card = self._get_or_create_card("enemy", i, show_skill=bool(getattr(p, "is_self", False)))
            card.show()
            self._fill_player_card(card, p)
        for i in range(len(session.their_team), len(self.enemy_cards)):
            self.enemy_cards[i].hide()

