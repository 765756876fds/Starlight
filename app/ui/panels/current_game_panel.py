"""当前对局面板：双排卡片布局，左己方右敌方。"""
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.event_bus import Event, get_event_bus
from app.lol.data.champion_catalog import ChampionCatalog
from app.lol.ongoing.champion_mastery import ChampionMasteryProvider
from app.lol.ongoing.intelligence import PlayerIntelligenceService
from app.lol.ongoing.intelligence_service import OngoingIntelligenceService
from app.lol.ongoing.sgp_provider import SgpMatchHistoryProvider
from app.lol.ongoing.sgp_server import SgpServerResolver
from app.lol.ongoing.debug_tracer import _DEBUG_ONGOING, ongoing_logger
from app.lol.title import compute_title
from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens

_FONT_SZ = 14          # 卡片正文主字号（px）
_FONT_SZ_STREAK = 13   # 连胜连败标记字号（比正文略小）

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
    """单个玩家卡片组件（4 行：名字 / 称号行 / 最近英雄 / 本局）"""
    def __init__(self, is_ally=True, parent=None):
        super().__init__(parent)
        self.is_ally = is_ally
        # 默认边框色：己方蓝 / 敌方红；命中称号时边框随称号色（update_data 每帧动态）
        self._border_default = Colors.ACCENT if is_ally else Colors.DANGER
        self.setStyleSheet(f"""
            PlayerCard {{
                border: 1px solid {self._border_default};
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
        self.lbl_name.setStyleSheet(f"font-weight: bold; color: white; font-size: {_FONT_SZ}px;")
        self.lbl_streak = QLabel("")
        self.lbl_streak.setStyleSheet(f"font-size: {_FONT_SZ_STREAK}px;")
        self.row1.addWidget(self.lbl_name, 1)
        self.row1.addWidget(self.lbl_streak)
        layout.addLayout(self.row1)

        self.lbl_line2 = QLabel("称号 · 位置 · 近N场")
        self.lbl_line2.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: {_FONT_SZ}px;")
        layout.addWidget(self.lbl_line2)

        self.lbl_recent = QLabel("最近: -")
        self.lbl_recent.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {_FONT_SZ}px;")
        layout.addWidget(self.lbl_recent)

        self.lbl_current = QLabel("本局: -")
        self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: {_FONT_SZ}px;")
        layout.addWidget(self.lbl_current)

    def update_data(self, name, level, line2_text, line2_color, is_mastery, streak_text="",
                    champion_zh="", recent_top="", title_text="", title_color=""):
        self.lbl_name.setText(f"{name}  Lv.{level}")
        self.lbl_name.setStyleSheet(f"font-weight: bold; color: white; font-size: {_FONT_SZ}px;")  # 每帧默认（整卡换色后恢复）
        if streak_text:
            if "胜" in streak_text:
                self.lbl_streak.setStyleSheet(
                    f"color: {Colors.SUCCESS}; font-size: {_FONT_SZ_STREAK}px; font-weight: bold;")
            else:
                self.lbl_streak.setStyleSheet(
                    f"color: {Colors.DANGER}; font-size: {_FONT_SZ_STREAK}px; font-weight: bold;")
            self.lbl_streak.setText(streak_text)
        else:
            self.lbl_streak.setText("")

        # line2 整行：称号/段位 · 位置 · 近N场 X胜Y负 KDA Z
        # 过渡态（line2_color=SECONDARY）灰字不加粗；称号行加粗；正常无称号主色不加粗
        self.lbl_line2.setText(line2_text)
        if line2_color == Colors.TEXT_SECONDARY:
            self.lbl_line2.setStyleSheet(
                f"color: {line2_color}; font-size: {_FONT_SZ}px;")
        elif line2_color:
            self.lbl_line2.setStyleSheet(
                f"color: {line2_color}; font-size: {_FONT_SZ}px; font-weight: bold;")
        else:
            self.lbl_line2.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; font-size: {_FONT_SZ}px;")

        if recent_top:
            self.lbl_recent.setText(f"最近: {recent_top}")
        else:
            self.lbl_recent.setText("最近: -")
        self.lbl_recent.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {_FONT_SZ}px;")  # 每帧默认

        if champion_zh:
            if is_mastery is True:
                self.lbl_current.setText(f"本局: {champion_zh}  ✅ 是绝活")
                self.lbl_current.setStyleSheet(f"color: {Colors.SUCCESS}; font-size: {_FONT_SZ}px;")
            elif is_mastery is False:
                self.lbl_current.setText(f"本局: {champion_zh}  ❌ 非绝活")
                self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {_FONT_SZ}px;")
            else:
                self.lbl_current.setText(f"本局: {champion_zh}  绝活: 未知")
                self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: {_FONT_SZ}px;")
        else:
            self.lbl_current.setText("本局: -")

        # 整卡换色：命中称号 → 所有文字用称号色（保留各 label 原字号/字重）+ 边框随称号色
        if title_color:
            self.lbl_name.setStyleSheet(f"font-weight: bold; color: {title_color}; font-size: {_FONT_SZ}px;")
            self.lbl_streak.setStyleSheet(f"font-size: {_FONT_SZ_STREAK}px; font-weight: bold; color: {title_color};")
            self.lbl_line2.setStyleSheet(f"font-size: {_FONT_SZ}px; font-weight: bold; color: {title_color};")
            self.lbl_recent.setStyleSheet(f"font-size: {_FONT_SZ}px; color: {title_color};")
            self.lbl_current.setStyleSheet(f"font-size: {_FONT_SZ}px; color: {title_color};")
        # 边框：称号色 → 随称号色；无称号 → 己方蓝/敌方红
        border_c = title_color or self._border_default
        self.setStyleSheet(f"""
            PlayerCard {{
                border: 1px solid {border_c};
                border-radius: 6px;
                padding: 6px;
                margin: 2px;
                background: rgba(30, 30, 50, 0.8);
            }}
        """)


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
                print("[UI] 玩家情报服务已初始化（SGP 动态懒加载）")
            except Exception as e:
                print(f"[UI] 玩家情报服务初始化失败: {e}")
                self._publish_intel_event("intel.init_failed", {
                    "error": str(e),
                    "error_type": type(e).__name__,
                })

        root = QVBoxLayout(self)
        root.setContentsMargins(Tokens.SPACING_SM, Tokens.SPACING_SM,
                                Tokens.SPACING_SM, Tokens.SPACING_SM)
        root.setSpacing(Tokens.SPACING_SM)

        # 标题
        self.lbl_title = QLabel("未在对局中")
        self.lbl_title.setStyleSheet(f"font-size: {Tokens.FONT_SIZE_BODY}px; font-weight: bold; color: {Tokens.TEXT_PRIMARY}")
        root.addWidget(self.lbl_title)

        # 左右两列标题
        cols_header = QHBoxLayout()
        self.lbl_ally_title = QLabel("🟦 己方")
        self.lbl_ally_title.setStyleSheet(f"color: {Tokens.ACCENT}; font-weight: bold; font-size: {Tokens.FONT_SIZE_BODY}px;")
        self.lbl_enemy_title = QLabel("🔴 敌方")
        self.lbl_enemy_title.setStyleSheet(f"color: {Tokens.DANGER}; font-weight: bold; font-size: {Tokens.FONT_SIZE_BODY}px;")
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

        self._last_render_key = None  # 差分渲染：上一帧渲染指纹（身份/实时/情报状态）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.stop()

    def _try_create_sgp_provider(self):
        # 优先走 OngoingGameService 的独立直连 getter（绕开共享连接断连窗口），
        # 与 loader 的 SGP 通道同款；拿不到再退化共享 lcu_get（当前行为）。
        getter = None
        if self._ongoing is not None:
            getter = getattr(self._ongoing, "get_direct_getter", lambda: None)()
        getter = getter or self._lcu_get
        if not getter:
            self._publish_intel_event("intel.sgp_provider_failed", {"error": "no_lcu_getter"})
            return None
        try:
            resolver = SgpServerResolver(getter)
            _, sgp_base, _ = resolver.resolve()
            if not sgp_base:
                # 静默失败路径：resolver 解析不到 base（平台 id 不在表/请求失败）——发事件防盲区
                self._publish_intel_event("intel.sgp_provider_failed", {"error": "resolver_no_base"})
                return None

            def _get_token():
                try:
                    r = getter("/entitlements/v1/token")
                    return r.get("accessToken") if isinstance(r, dict) else None
                except Exception:
                    return None

            print(f"[UI] SGP provider 动态初始化成功: {sgp_base}")
            self._publish_intel_event("intel.sgp_provider_ready", {"sgp_base": sgp_base})
            return SgpMatchHistoryProvider(
                entitlements_token=_get_token,
                sgp_base=lambda: sgp_base,
            )
        except Exception as e:
            print(f"[UI] SGP provider 动态初始化失败: {e}")
            self._publish_intel_event("intel.sgp_provider_failed", {"error": str(e)})
            return None

    def _get_or_create_card(self, team, idx):
        cards = self.ally_cards if team == "ally" else self.enemy_cards
        layout = self.ally_layout if team == "ally" else self.enemy_layout
        while len(cards) <= idx:
            card = PlayerCard(is_ally=(team == "ally"))
            cards.append(card)
            layout.addWidget(card)
        return cards[idx]

    def _ensure_intel(self, puuid: str, champion_id: int):
        """懒触发玩家情报查询（后台线程，防重复）。"""
        if not puuid or self._intelligence is None:
            return
        if self._intelligence.should_throttle(puuid):
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

    def _publish_intel_event(self, event_type: str, data: dict) -> None:
        """intel/SGP 链路事件（进 data/events/ 日志，补 print 盲区）。"""
        try:
            get_event_bus().publish(Event(
                event_type=event_type,
                data=data,
                source="ui",
            ))
        except Exception:
            pass

    def _fetch_intel(self, puuid: str, champion_id: int):
        try:
            self._intelligence.get_player_intelligence(puuid, champion_id)
        except Exception as e:
            print(f"[UI] 玩家情报查询失败 {puuid}: {e}")
            # 只 print 会丢日志（控制台关掉就没）→ 同时发事件，data/events/ 可查
            self._publish_intel_event("intel.query_failed", {
                "puuid": puuid,
                "champion_id": champion_id,
                "error": str(e),
            })
        finally:
            with self._loading_lock:
                self._loading_puuids.discard(puuid)

    def _fill_player_card(self, card, player, early_loading: bool = False):
        """纯渲染：从 CurrentGamePlayer 三区读取（identity=gameflow / realtime=2999 / history=loader）。

        不再做任何身份匹配、缓存反查、历史/绝活查询或兜底。
        early_loading：进游戏初期（InProgress/GameStart 前 15s）且 2999 未回填英雄 ID 时的兜底提示。
        """
        snap_data = player.history  # PlayerSnapshot（loader 单链产物）或 None（尚未加载完成）
        is_self = bool(getattr(player, "is_self", False))

        name = player.display_name or "?"
        if is_self:
            name = f"你: {name}"

        # 本局英雄：identity.champion_id（gameflow 权威）→ 目录转中文；
        # 选人阶段未锁定（champion_id==0）时回退 champion_pick_intent 预选英雄，标记"(预选)"；
        # 进游戏初期 2999 真空期无任何 ID 时显示"加载中"，不干巴巴显示 ?
        champ = "?"
        display_cid = getattr(player, "champion_id", 0)
        is_intent = False
        if display_cid == 0 and getattr(player, "champion_pick_intent", 0):
            display_cid = player.champion_pick_intent
            is_intent = True
        if display_cid:
            c = self._catalog.get_by_id(display_cid) if self._catalog else None
            champ = c.name_zh if c and c.name_zh else (c.en_name if c else f"#{display_cid}")
            if is_intent:
                champ += " (预选)"
        elif early_loading:
            champ = "加载中"  # 进游戏初期 2999 真空期兜底（数据就绪后下一帧自动替换）

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

        line2_state = ""        # "近N场 X胜Y负 KDA Z"（intel/loader 就绪时）
        line2_transition = ""   # 过渡态文案（未就绪时整个 line2 用它）
        # B 行情报（intel）就绪判定：提前，供战绩显示与称号判定同源（候选 A）
        puuid = getattr(player, "puuid", "") or ""
        intel_entry = None
        if puuid and self._intelligence is not None:
            self._ensure_intel(puuid, getattr(player, "champion_id", 0) or 0)
            intel_entry = self._intelligence.get_cached(puuid)
        intel_ready = (intel_entry is not None
                       and intel_entry.recent_status == "AVAILABLE"
                       and intel_entry.recent_games >= 1)
        if intel_ready:
            wins = intel_entry.recent_wins
            losses = intel_entry.recent_losses
            total = intel_entry.recent_games
            line2_state = f"近{total}场 {wins}胜{losses}负 KDA {intel_entry.recent_average_kda:.2f}"
            if intel_entry.recent_unknown_count > 0:
                line2_state += f"（{intel_entry.recent_unknown_count}场数据不全）"
        else:
            # intel 未就绪：回退 loader 显示（过渡态，称号此时也不评）
            hist_ready = (snap_data is not None
                          and getattr(snap_data, "data_state", None) == "AVAILABLE"
                          and getattr(snap_data, "recent_matches", None))
            if snap_data is not None and not hist_ready and getattr(snap_data, "data_state", None) != "AVAILABLE":
                if getattr(snap_data, "data_state", None) == "NO_DATA":
                    # 请求成功但全是训练/娱乐模式（人机等）：确定的无匹配/排位战绩，不转圈
                    line2_transition = "无匹配/排位战绩"
                else:
                    # 请求失败：自动重试中，显示获取中而不是误导性的0/0/0
                    line2_transition = "战绩获取中…（自动重试）"
            elif hist_ready:
                wins = sum(1 for m in snap_data.recent_matches if m.win is True)
                losses = sum(1 for m in snap_data.recent_matches if m.win is False)
                total = len(snap_data.recent_matches)
                unknown = total - wins - losses
                kda_parts = [
                    (m.kills + m.assists) / m.deaths
                    for m in snap_data.recent_matches
                    if m.kills is not None and m.deaths is not None
                    and m.assists is not None and m.deaths > 0
                ]
                if kda_parts:
                    kda = sum(kda_parts) / len(kda_parts)
                    line2_state = f"近{total}场 {wins}胜{losses}负 KDA {kda:.2f}"
                else:
                    line2_state = f"近{total}场 {wins}胜{losses}负 KDA --"
                if unknown > 0:
                    line2_state += f"（{unknown}场数据不全）"
            elif snap_data is None:
                # history 尚未加载完成：实时 KDA 过渡（身份/玩家存在性不受影响）
                line2_transition = f"战绩加载中… 实时 {k}/{d}/{a}"
            else:
                line2_transition = f"{k}/{d}/{a}"
                if d > 0:
                    line2_transition += f" (KDA {(k + a) / d:.2f})"
                else:
                    line2_transition += " (KDA --)"

        is_mastery = None
        rank_text = ""
        title_text = ""
        title_color = ""
        streak_text = ""

        # 段位（历史）：只显示大段 + 小段（"翡翠 IV"）；LP/flex 详情不再显示（称号场景优先）
        if snap_data is not None and getattr(snap_data, "ranked_tier", None):
            tier = snap_data.ranked_tier
            rank_text = _TIER_ZH.get(tier.upper(), tier)
            if getattr(snap_data, "ranked_division", None):
                rank_text += f" {snap_data.ranked_division}"

        # B 行（最近状态，所有人）：懒触发 + 缓存读
        recent_top = ""
        if puuid and self._intelligence is not None:
            if intel_entry is None:
                with self._loading_lock:
                    loading_now = puuid in self._loading_puuids
                recent_top = "获取中…" if loading_now else "-"
            elif intel_entry.recent_status == "AVAILABLE":
                stats = [c for c in (intel_entry.champion_stats or []) if c.games >= 5][:3]
                if stats:
                    parts = []
                    for c in stats:
                        # SGP championName 为英文 slug（如 Thresh），统一映射中文称号
                        cc = self._catalog.get_by_id(c.champion_id) if self._catalog else None
                        cname_zh = cc.name_zh if cc and cc.name_zh else c.champion_name
                        parts.append(f"{cname_zh} {int(c.win_rate * 100)}%")
                    recent_top = " · ".join(parts)
                else:
                    recent_top = "样本不足"
            else:
                recent_top = "-"

            # 修仙称号（替换段位行左部）：intel 就绪才评（无有效场次 → 不评，line2 回退原段位行）
            if intel_entry is not None and intel_entry.recent_status == "AVAILABLE":
                tier_raw = getattr(snap_data, "ranked_tier", None) if snap_data is not None else None
                tr = compute_title(
                    win_rate=intel_entry.recent_win_rate,
                    average_kda=intel_entry.recent_average_kda,
                    streak=intel_entry.streak,
                    utility_ratio=intel_entry.utility_ratio,
                    tier=tier_raw,          # 有段位 → "X境·"前缀；None → 纯称号（8 档同名）
                )
                title_text = tr.text
                title_color = tr.color

        # line2 整行：称号/段位 · 位置 · 近N场 X胜Y负 KDA Z；过渡态灰字整体替换
        if line2_transition:
            line2_text = line2_transition
            line2_color = Colors.TEXT_SECONDARY
        else:
            parts = []
            if title_text:
                parts.append(title_text)
            elif rank_text:
                parts.append(rank_text)
            if pos_zh:
                parts.append(pos_zh)
            if line2_state:
                parts.append(line2_state)
            line2_text = " · ".join(parts) if parts else "-"
            line2_color = title_color or Colors.TEXT_PRIMARY

        # 本局是否绝活：LCU Champion Mastery Top3 权威判定（生涯熟练度）
        # intel_entry 已在 L352-355 提前算好；None/非 AVAILABLE 时字段保持 None（UI 显示“未知”）
        if intel_entry is not None:
            is_mastery = intel_entry.current_champion_is_mastery_top3

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
            line2_text=line2_text,
            line2_color=line2_color,
            is_mastery=is_mastery,
            streak_text=streak_text,
            champion_zh=champ,
            recent_top=recent_top,
            title_text=title_text,
            title_color=title_color,
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
            self._last_render_key = None
            self.lbl_title.setText("未在对局中 / 选人中")
            for card in self.ally_cards + self.enemy_cards:
                card.hide()
            return

        # 差分渲染：身份/实时/情报状态与上一帧完全一致时跳过全量重绘（防闪烁）。
        # 覆盖：名字、英雄 ID、KDA/位置/等级、history 就绪、intel（绝活/称号）就绪；
        # 标题时间按 5s 粒度纳入，避免加载初期标题时间冻结。
        render_key = tuple(
            (
                getattr(p, "puuid", "") or "",
                getattr(p, "display_name", "") or "",
                getattr(p, "champion_id", 0) or 0,
                getattr(p, "champion_pick_intent", 0) or 0,
                getattr(p, "live_kills", 0) or 0,
                getattr(p, "live_deaths", 0) or 0,
                getattr(p, "live_assists", 0) or 0,
                getattr(p, "live_position", "") or "",
                getattr(p, "live_level", 0) or 0,
                getattr(p, "history", None) is not None,
                (self._intelligence is not None
                 and self._intelligence.get_cached(getattr(p, "puuid", "") or "") is not None),
            )
            for p in session.players
        ) + (int(game_time // 5),)
        if render_key == self._last_render_key:
            if _DEBUG_ONGOING:
                ongoing_logger.debug("UI Refresh skipped by fingerprint.")
            return

        if _DEBUG_ONGOING:
            ongoing_logger.debug("UI Refresh redraw triggered!")
        # 进游戏初期（InProgress/GameStart 前 15s）2999 真空期兜底标志：
        # 英雄 ID 未回填时显示"加载中"（预选英雄优先兜底），数据就绪后下一帧自动替换
        early_loading = (
            getattr(session, "phase", "") in ("InProgress", "GameStart")
            and (time.time() - getattr(session, "started_at", time.time())) < 15
        )
        n = len(session.players)
        self.lbl_title.setText(f"当前对局  时间 {_fmt_game_time(game_time)}  玩家 {n}")

        # 冻结更新：整帧渲染期间禁止中间态重绘（Qt 防闪烁标准做法），
        # 全部卡片 setText/QSS 完成后一次性 update，保证一帧呈现最终状态
        self.setUpdatesEnabled(False)
        try:
            for i, p in enumerate(session.my_team):
                card = self._get_or_create_card("ally", i)
                card.show()
                self._fill_player_card(card, p, early_loading)
            for i in range(len(session.my_team), len(self.ally_cards)):
                self.ally_cards[i].hide()

            for i, p in enumerate(session.their_team):
                card = self._get_or_create_card("enemy", i)
                card.show()
                self._fill_player_card(card, p, early_loading)
            for i in range(len(session.their_team), len(self.enemy_cards)):
                self.enemy_cards[i].hide()
        finally:
            self.setUpdatesEnabled(True)
            self.update()

        self._last_render_key = render_key

