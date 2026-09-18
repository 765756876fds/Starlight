"""对局信息面板：展示 ChampSelect 10 玩家快照。"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


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
    if snap.load_error:
        lay.addWidget(QLabel("<i>数据不可用</i>"))
    else:
        wins = sum(1 for m in snap.recent_matches if m.win)
        total = len(snap.recent_matches) or 1
        kda = f"{snap.kda_ratio:.2f}"
        lay.addWidget(QLabel(f"近{total}场: {wins}胜 / {total-wins}负  KDA={kda}"))
        row = QLabel()
        colors = []
        for m in snap.recent_matches[:5]:
            colors.append("#2e7d32" if m.win else "#c62828")
        row.setText("".join(
            f'<span style="background:{c}; color:white; padding:2px 6px;">{"W" if i<len(colors) and snap.recent_matches[i].win else "L"}</span> '
            for i, c in enumerate(colors)
        ))
        lay.addWidget(row)
    return card


class OngoingPanel(QWidget):
    def __init__(self, ongoing_service=None, parent=None):
        super().__init__(parent)
        self._svc = ongoing_service
        root = QVBoxLayout(self)

        self.lbl_hint = QLabel("⚠️ 战绩信息来自公开数据，仅供参考")
        self.lbl_hint.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(self.lbl_hint)

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
