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
    dlg = QDialog()
    dlg.setWindowTitle("对局详情")
    lay = QVBoxLayout(dlg)
    dt = datetime.datetime.fromtimestamp(m.game_creation / 1000).strftime("%Y-%m-%d %H:%M") if m.game_creation else "--"
    dur = f"{m.game_duration // 60}分{m.game_duration % 60}秒" if m.game_duration else "--"
    win = "胜利" if m.win else "失败"
    k = m.kills if m.kills is not None else "-"
    d = m.deaths if m.deaths is not None else "-"
    a = m.assists if m.assists is not None else "-"
    for r in [
        f"<b>{_zh(m.champion_name or '?')}</b>  {win}",
        f"时间: {dt}", f"模式: {m.queue_type or '未知'}", f"时长: {dur}",
        f"K/D/A: {k} / {d} / {a}",
        f"补刀: {m.cs if m.cs is not None else '--'}",
        f"金币: {m.gold if m.gold is not None else '--'}",
        f"位置: {_zh_pos(m.team_position) or '--'}",
    ]:
        lay.addWidget(QLabel(r))
    # 10 人详情（SGP 单场 SUMMARY）
    if svc is not None and m.game_id:
        try:
            pu = svc.self_puuid()
            det = svc.get_detail(m.game_id, pu)
            lay.addWidget(QLabel("<b>双方阵容</b>"))
            for team, title in [(100, "蓝方"), (200, "红方")]:
                lay.addWidget(QLabel(title))
                for p in [x for x in det.players if x.team_id == team]:
                    lay.addWidget(QLabel(
                        f"  {p.name or '?'}  {_zh(p.champion_name)} {_zh_pos(p.position)} "
                        f"{p.kills}/{p.deaths}/{p.assists}  补刀{p.cs}  金币{p.gold}"
                    ))
        except Exception:
            lay.addWidget(QLabel("<i>详细数据暂时不可用</i>"))
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
