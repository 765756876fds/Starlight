"""对局信息面板：展示 ChampSelect 10 玩家快照。"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QFrame, QLineEdit, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)
import datetime, os, json


def _zh_map() -> dict:
    try:
        p = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                         "data", "lol_data", "champions.json")
        d = json.load(open(p, encoding="utf-8"))
        return {c.get("alias"): c.get("name") for c in d}
    except Exception:
        return {}


_ZH = _zh_map()

_SPELL_ZH = {}
try:
    _sp = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                 "data", "lol_data", "summoner_spells.json"), encoding="utf-8"))
    _SPELL_ZH = {s["sid"]: s["name_zh"] for s in _sp.get("spells", [])}
except Exception:
    pass

_RUNE_TREE = {8000: "精密", 8100: "主宰", 8200: "巫术", 8300: "坚决", 8400: "启迪"}

_ITEM_ZH = {}
try:
    _it = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                  "data", "lol_data", "items.json"), encoding="utf-8"))
    _ITEM_ZH = {int(k): v for k, v in _it.get("items", {}).items()}
except Exception:
    pass

def _item_zh(iid):
    if not iid:
        return ""
    return _ITEM_ZH.get(int(iid), str(iid))

def _spell_zh(sid):
    if not sid:
        return ""
    return _SPELL_ZH.get(sid, str(sid))

def _multikill_zh(p):
    if (p.penta_kills or 0) > 0: return f"五杀x{p.penta_kills}"
    if (p.quadra_kills or 0) > 0: return f"四杀x{p.quadra_kills}"
    if (p.triple_kills or 0) > 0: return f"三杀x{p.triple_kills}"
    if (p.double_kills or 0) > 0: return f"双杀x{p.double_kills}"
    return ""


_EVT_ZH = {
    "LEVEL_UP": "升级", "SKILL_LEVEL_UP": "技能升级",
    "ITEM_PURCHASED": "购买装备", "ITEM_SOLD": "出售装备",
    "ITEM_DESTROYED": "装备销毁", "ITEM_UNDO": "撤销购买",
    "WARD_PLACED": "放置眼位", "WARD_KILL": "摧毁眼位",
    "CHAMPION_KILL": "击杀英雄", "CHAMPION_SPECIAL_KILL": "特殊击杀",
    "BUILDING_KILL": "摧毁建筑", "ELITE_MONSTER_KILL": "击杀大型野怪",
    "DRAGON_SOUL_GIVEN": "获得龙魂", "OBJECTIVE_BOUNTY_PRESTART": "赏金目标",
    "TURRET_PLATE_DESTROYED": "摧毁镀层", "GAME_END": "游戏结束", "PAUSE_END": "继续",
}

_MONSTER_ZH = {
    "DRAGON": "巨龙", "ELDER_DRAGON": "远古巨龙", "ELDER_DRAGON": "远古巨龙",
    "BARON_NASHOR": "纳什男爵", "BARON": "纳什男爵",
    "RIFTHERALD": "峡谷先锋", "HORDE": "虚空虫群", "ATAKHAN": "亚塔坎",
}
_BUILD_ZH = {
    "TOWER_BUILDING": "防御塔", "INHIBITOR_BUILDING": "水晶枢纽", "NEXUS_BUILDING": "召唤水晶",
    "OUTER_TURRET": "外塔", "INNER_TURRET": "二塔", "BASE_TURRET": "高地塔", "NEXUS_TURRET": "基地塔",
}

def _monster_zh(m):
    return _MONSTER_ZH.get(m, m or "大型野怪")

def _build_zh(b):
    return _BUILD_ZH.get(b, b or "建筑")

_HIGH_VALUE = {"CHAMPION_KILL","CHAMPION_SPECIAL_KILL","BUILDING_KILL","ELITE_MONSTER_KILL",
               "DRAGON_SOUL_GIVEN","TURRET_PLATE_DESTROYED","ITEM_PURCHASED","ITEM_SOLD",
               "WARD_KILL","GAME_END"}
from app.lol.ongoing.match_detail import MatchTimelineService, _fmt_ms

def _runes_zh(p):
    parts = []
    if p.primary_style: parts.append(_RUNE_TREE.get(p.primary_style, str(p.primary_style)))
    if p.secondary_style: parts.append(_RUNE_TREE.get(p.secondary_style, str(p.secondary_style)))
    return "/".join(parts) if parts else "--"

_POS_ZH = {
    "TOP": "上路",
    "JUNGLE": "打野",
    "MIDDLE": "中路",
    "BOTTOM": "下路",
    "UTILITY": "辅助",
    "NONE": "",
    "": "",
}


def _zh_pos(p):
    if not p:
        return ""
    s = str(p).strip().upper()
    return _POS_ZH.get(s, str(p))


def _zh(name: str) -> str:
    return _ZH.get(name, name)


def _show_detail(m, svc=None) -> None:
    from PySide6.QtWidgets import QTabWidget
    dlg = QDialog()
    dlg.setWindowTitle("对局详情")
    dlg.resize(520, 720)
    root = QVBoxLayout(dlg)
    dt = datetime.datetime.fromtimestamp(m.game_creation / 1000).strftime("%Y-%m-%d %H:%M") if m.game_creation else "--"
    dur = f"{m.game_duration // 60}分{m.game_duration % 60}秒" if m.game_duration else "--"
    win = "胜利" if m.win else "失败"
    k = m.kills if m.kills is not None else "-"
    d = m.deaths if m.deaths is not None else "-"
    a = m.assists if m.assists is not None else "-"
    for r in [
        f"<b>{_zh(m.champion_name or '?')}</b>  {win}",
        f"时间: {dt}  模式: {m.queue_type or '未知'}  时长: {dur}",
        f"K/D/A: {k} / {d} / {a}   补刀: {m.cs if m.cs is not None else '--'}   金币: {m.gold if m.gold is not None else '--'}",
    ]:
        root.addWidget(QLabel(r))

    tabs = QTabWidget()
    root.addWidget(tabs)

    # ---- Tab 1: 阵容 ----
    squad = QWidget()
    squad_lay = QVBoxLayout(squad)
    if svc is not None and m.game_id:
        try:
            pu = svc.self_puuid()
            det = svc.get_detail(m.game_id, pu)
            squad_lay.addWidget(QLabel("<b>双方阵容</b>"))
            for team, title in [(100, "蓝方"), (200, "红方")]:
                squad_lay.addWidget(QLabel(title))
                for pl in [x for x in det.players if x.team_id == team]:
                    mk = _multikill_zh(pl)
                    spells = "/".join(x for x in [_spell_zh(pl.spell1), _spell_zh(pl.spell2)] if x)
                    items = " ".join(_item_zh(i) for i in (pl.items or []) if i)
                    dmg = f"  伤害{pl.dmg_to_champ or '--'}/承伤{pl.dmg_taken or '--'}"
                    vis = f"  视野{pl.vision_score or '--'}"
                    priv = ""
                    if getattr(pl, "privacy", "") == "PRIVATE":
                        priv = " [隐藏战绩]"
                    elif getattr(pl, "privacy", "") == "PUBLIC":
                        priv = " [战绩公开]"
                    squad_lay.addWidget(QLabel(
                        f"  {pl.name or '?'}{priv}  {_zh(pl.champion_name)} Lv{pl.level or '--'} {_zh_pos(pl.position)} "
                        f"{pl.kills}/{pl.deaths}/{pl.assists}  补刀{pl.cs}  金币{pl.gold}"))
                    squad_lay.addWidget(QLabel(
                        f"    符文{_runes_zh(pl)}  技能{spells or '--'}  {mk}{vis}{dmg}"))
                    squad_lay.addWidget(QLabel(f"    装备: {items or '--'}"))
        except Exception:
            det = None
            squad_lay.addWidget(QLabel("<i>详细数据暂时不可用</i>"))
    else:
        det = None
    squad_lay.addStretch(1)
    squad_scroll = QScrollArea(); squad_scroll.setWidgetResizable(True); squad_scroll.setWidget(squad)
    tabs.addTab(squad_scroll, "阵容")

    # ---- Tab 2: 事件时间线（切到该 tab 才加载，避免打开即卡）----
    tl = QWidget()
    tl_lay = QVBoxLayout(tl)
    tl_lay.addWidget(QLabel("<i>点击此页加载事件时间线…</i>"))
    tl_lay.addStretch(1)
    tl_scroll = QScrollArea(); tl_scroll.setWidgetResizable(True); tl_scroll.setWidget(tl)
    tabs.addTab(tl_scroll, "事件时间线")

    def load_timeline():
        if getattr(load_timeline, "done", False):
            return
        load_timeline.done = True
        # 清空提示
        while tl_lay.count():
            it = tl_lay.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        if not (svc is not None and m.game_id):
            tl_lay.addWidget(QLabel("<i>暂无可加载数据</i>")); tl_lay.addStretch(1); return
        try:
            from app.lol.ongoing.sgp_server import SgpServerResolver
            _, _, sub = SgpServerResolver(svc._lcu_get).resolve()
            events, pid_puuid = MatchTimelineService(svc._lcu_get).get_events(m.game_id, sub)
            pu_name = {pl.puuid: (pl.name or "?") for pl in (det.players if det else [])}
            def nm(pid):
                if not pid: return "?"
                return pu_name.get(pid_puuid.get(pid, ""), f"P{pid}")
            shown = [e for e in events if e.event_type in _HIGH_VALUE]
            if not shown:
                tl_lay.addWidget(QLabel("<i>暂无事件数据</i>"))
            for e in shown:
                zh = _EVT_ZH.get(e.event_type, f"未知事件({e.event_type})")
                if e.event_type == "CHAMPION_KILL":
                    txt = f"{nm(e.killer_id)} 击杀 {nm(e.victim_id)}"
                    if e.assist_ids:
                        txt += " 助攻:" + ",".join(nm(a) for a in e.assist_ids)
                elif e.event_type in ("ITEM_PURCHASED","ITEM_SOLD"):
                    txt = f"{nm(e.participant_id)} {zh} {_item_zh(e.item_id) or ('装备'+str(e.item_id))}"
                elif e.event_type == "BUILDING_KILL":
                    txt = f"{nm(e.killer_id)} 摧毁 {_build_zh(e.building_type)}"
                elif e.event_type == "ELITE_MONSTER_KILL":
                    txt = f"{nm(e.killer_id)} 击杀 {_monster_zh(e.monster_type)}"
                elif e.event_type == "WARD_KILL":
                    txt = f"{nm(e.killer_id)} 摧毁眼位"
                else:
                    txt = zh
                tl_lay.addWidget(QLabel(f"  {_fmt_ms(e.timestamp_ms)}  {txt}"))
            tl_lay.addStretch(1)
        except Exception:
            tl_lay.addWidget(QLabel("<i>事件时间线暂不可用</i>"))
            tl_lay.addStretch(1)

    tabs.currentChanged.connect(lambda idx: load_timeline() if idx == 1 else None)
    dlg.exec()


def _match_row(m, svc=None) -> QPushButton:
    dt = datetime.datetime.fromtimestamp(m.game_creation / 1000).strftime("%m-%d") if m.game_creation else "--"
    win = "胜" if m.win else "负"
    k = m.kills if m.kills is not None else "-"
    d = m.deaths if m.deaths is not None else "-"
    a = m.assists if m.assists is not None else "-"
    btn = QPushButton(f"{win}  {dt}  {m.queue_type or ''}  {_zh(m.champion_name or '?')}  {k}/{d}/{a}")
    btn.setStyleSheet("text-align:left; border:none; padding:2px; color:#ddd;")
    btn.setFlat(True)
    btn.clicked.connect(lambda: _show_detail(m, svc))
    return btn


def _card(snap, is_enemy: bool) -> QFrame:
    card = QFrame()
    card.setFrameShape(QFrame.StyledPanel)
    border = "border: 2px solid #c62828;" if is_enemy else "border: 2px solid #1565c0;"
    card.setStyleSheet(border + " border-radius: 6px; padding: 6px;")
    lay = QVBoxLayout(card)

    lay.addWidget(QLabel(f"<b>{snap.display_name}</b>  Lv.{snap.summoner_level}"))
    if snap.ranked_tier:
        tier = snap.ranked_tier
        if snap.ranked_division:
            tier = f"{tier} {snap.ranked_division} {snap.ranked_lp}LP"
        lay.addWidget(QLabel(f"段位: {tier}"))
    else:
        lay.addWidget(QLabel("段位: 未定级/不可用"))
    state = getattr(snap, "data_state", "ERROR")
    if state == "ERROR":
        lay.addWidget(QLabel("<i>近期战绩：数据不可用</i>"))
    elif state == "NO_DATA":
        lay.addWidget(QLabel("<i>近期战绩：暂无对局记录</i>"))
    else:
        wins = sum(1 for m in snap.recent_matches if m.win)
        total = len(snap.recent_matches)
        kda = f"{snap.kda_ratio:.2f}"
        lay.addWidget(QLabel(f"近{total}场: {wins}胜 / {total-wins}负  KDA={kda}"))
        row = QLabel()
        colors = ["#2e7d32" if m.win else "#c62828" for m in snap.recent_matches[:5]]
        row.setText("".join(
            f'<span style="background:{c}; color:white; padding:2px 6px;">{"W" if snap.recent_matches[i].win else "L"}</span> '
            for i, c in enumerate(colors)
        ))
        lay.addWidget(row)
    return card


class OngoingPanel(QWidget):
    def __init__(self, ongoing_service=None, account_history_service=None, parent=None):
        super().__init__(parent)
        self._svc = ongoing_service
        self._acct = account_history_service
        self._acct_next = 0.0
        root = QHBoxLayout(self)

        # 左侧导航
        self.nav = QListWidget()
        self.nav.setFixedWidth(110)
        self.nav.addItem("我的战绩")
        self.nav.addItem("当前对局")
        root.addWidget(self.nav)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        # ---- Page 0: 我的战绩 ----
        page_acct = QWidget()
        pl = QVBoxLayout(page_acct)
        pl.setContentsMargins(8, 8, 8, 8)
        self.lbl_hint = QLabel("⚠️ 战绩信息来自公开数据，仅供参考")
        self.lbl_hint.setStyleSheet("color: #888; font-size: 11px;")
        pl.addWidget(self.lbl_hint)

        head = QHBoxLayout()
        head.addWidget(QLabel("<b>我的近期战绩</b>"))
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._force_refresh_account)
        head.addWidget(self.btn_refresh)
        head.addStretch()
        pl.addLayout(head)
        pl.addWidget(QLabel("<b>查询玩家战绩（Riot ID：名称#标签）</b>"))
        srow = QHBoxLayout()
        self.in_search = QLineEdit()
        self.in_search.setPlaceholderText("例如：果子#48230")
        self.in_search.returnPressed.connect(self._do_search)
        btn = QPushButton("搜索")
        btn.clicked.connect(self._do_search)
        srow.addWidget(self.in_search); srow.addWidget(btn)
        pl.addLayout(srow)
        self._search_status = QLabel("")
        pl.addWidget(self._search_status)
        self._search_box = QVBoxLayout()
        pl.addLayout(self._search_box)
        self._acct_box = QVBoxLayout()
        self._acct_status = QLabel("")
        self._acct_box.addWidget(self._acct_status)
        pl.addLayout(self._acct_box)
        pl.addStretch()

        acct_scroll = QScrollArea()
        acct_scroll.setWidgetResizable(True)
        acct_scroll.setWidget(page_acct)
        self.stack.addWidget(acct_scroll)

        # ---- Page 1: 当前对局 ----
        page_cur = QWidget()
        cl = QVBoxLayout(page_cur)
        cl.setContentsMargins(8, 8, 8, 8)
        self.lbl_status = QLabel("未进入选人阶段")
        cl.addWidget(self.lbl_status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._inner = QWidget()
        self._grid = QGridLayout(self._inner)
        self.scroll.setWidget(self._inner)
        cl.addWidget(self.scroll)
        self.stack.addWidget(page_cur)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def refresh(self):
        self._refresh_account()
        if self._svc is None:
            return
        try:
            snaps = self._svc.get_snapshots()
        except Exception:
            return
        # 清空 grid
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not snaps:
            self.lbl_status.setText("未进入选人阶段")
            return

        my = [s for s in snaps if s.team == "myTeam"]
        en = [s for s in snaps if s.team == "theirTeam"]
        self.lbl_status.setText(f"已加载 {len(snaps)}/10")
        row = 0
        self._grid.addWidget(QLabel("<b>我方</b>"), row, 0)
        row += 1
        for i, s in enumerate(my):
            self._grid.addWidget(_card(s, is_enemy=False), row, i)
        row += 1
        self._grid.addWidget(QLabel("<b>敌方</b>"), row, 0)
        row += 1
        for i, s in enumerate(en):
            self._grid.addWidget(_card(s, is_enemy=True), row, i)

    def _force_refresh_account(self):
        self._acct_next = 0.0
        self._refresh_account()

    def _do_search(self):
        from app.lol.ongoing.player_search import PlayerSearchService
        from app.lol.ongoing.player_search import parse_riot_id
        if not self._acct:
            self._search_status.setText("服务未就绪"); return
        txt = self.in_search.text()
        gn, tag = parse_riot_id(txt)
        if not gn:
            self._search_status.setText("请输入 名称#标签"); return
        self._search_status.setText("正在查询玩家…")
        try:
            svc = PlayerSearchService(self._acct._lcu_get, self._acct._lcu_post)
            r = svc.search(txt)
        except Exception as e:
            self._search_status.setText(f"查询失败：{e}"); return
        while self._search_box.count():
            it = self._search_box.takeAt(0); w=it.widget()
            if w: w.deleteLater()
        if r.status == "NOT_FOUND":
            self._search_status.setText("未找到玩家，请检查 Riot ID")
        elif r.status == "INVALID_RIOT_ID":
            self._search_status.setText("请输入 名称#标签")
        elif r.status == "SEARCH_UNAVAILABLE":
            self._search_status.setText("战绩数据源暂时不可用")
        elif not r.matches:
            self._search_status.setText("该玩家暂无可查询的近期战绩")
        else:
            priv_tag = ""
            if getattr(r, "privacy", "") == "PRIVATE":
                priv_tag = "  [隐藏战绩]"
            elif getattr(r, "privacy", "") == "PUBLIC":
                priv_tag = "  [战绩公开]"
            elif getattr(r, "privacy", "") == "ERROR":
                priv_tag = "  [隐私状态未知]"
            self._search_status.setText(f"{r.game_name}#{r.tag_line} 近期战绩{priv_tag}")
            for m in r.matches[:10]:
                self._search_box.addWidget(_match_row(m, self._acct))
    def _refresh_account(self):
        import time
        if self._acct is None:
            return
        now = time.time()
        if now < self._acct_next:
            return
        self._acct_next = now + 60.0
        try:
            res = self._acct.recent_matches()
        except Exception:
            self._acct_status.setText("近期战绩暂时不可用")
            return
        # 清空旧行
        while self._acct_box.count() > 1:
            it = self._acct_box.takeAt(1)
            w = it.widget()
            if w:
                w.deleteLater()
        if res.state == "AVAILABLE":
            wins = sum(1 for m in res.records if m.win)
            self._acct_status.setText(
                f"近{len(res.records)}场: {wins}胜/{len(res.records)-wins}负"
            )
            for m in res.records[:10]:
                self._acct_box.addWidget(_match_row(m, self._acct))
        elif res.state == "NO_DATA":
            self._acct_status.setText("暂无近期战绩")
        else:
            self._acct_status.setText("近期战绩暂时不可用")
