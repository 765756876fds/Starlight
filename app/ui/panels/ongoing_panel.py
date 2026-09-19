"""对局信息面板：展示 ChampSelect 10 玩家快照。"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
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


def _zh(name: str) -> str:
    return _ZH.get(name, name)


def _show_detail(m) -> None:
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
        f"位置: {m.team_position or '--'}",
    ]:
        lay.addWidget(QLabel(r))
    dlg.exec()


def _match_row(m) -> QPushButton:
    dt = datetime.datetime.fromtimestamp(m.game_creation / 1000).strftime("%m-%d") if m.game_creation else "--"
    win = "胜" if m.win else "负"
    k = m.kills if m.kills is not None else "-"
    d = m.deaths if m.deaths is not None else "-"
    a = m.assists if m.assists is not None else "-"
    btn = QPushButton(f"{win}  {dt}  {m.queue_type or ''}  {_zh(m.champion_name or '?')}  {k}/{d}/{a}")
    btn.setStyleSheet("text-align:left; border:none; padding:2px; color:#ddd;")
    btn.setFlat(True)
    btn.clicked.connect(lambda: _show_detail(m))
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
        root = QVBoxLayout(self)

        self.lbl_hint = QLabel("⚠️ 战绩信息来自公开数据，仅供参考")
        self.lbl_hint.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(self.lbl_hint)

        # 我的近期战绩（SGP），与下方 10 人当前对局互不影响
        root.addWidget(QLabel("<b>我的近期战绩</b>"))
        self._acct_box = QVBoxLayout()
        self._acct_status = QLabel("")
        self._acct_box.addWidget(self._acct_status)
        root.addLayout(self._acct_box)

        self.lbl_status = QLabel("未进入选人阶段")
        root.addWidget(self.lbl_status)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._inner = QWidget()
        self._grid = QGridLayout(self._inner)
        self.scroll.setWidget(self._inner)
        root.addWidget(self.scroll)

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
                self._acct_box.addWidget(_match_row(m))
        elif res.state == "NO_DATA":
            self._acct_status.setText("暂无近期战绩")
        else:
            self._acct_status.setText("近期战绩暂时不可用")
