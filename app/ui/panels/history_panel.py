"""历史对局面板：只读 HistoryRepository，字段缺失显示 UNKNOWN，不推测。"""
from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


def _fmt_time(ts) -> str:
    try:
        if not ts:
            return "UNKNOWN"
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "UNKNOWN"


def _fmt_dur(d) -> str:
    try:
        if not d:
            return "UNKNOWN"
        return f"{int(float(d)//60)}分{int(float(d)%60):02d}秒"
    except Exception:
        return "UNKNOWN"


class HistoryPanel(QWidget):
    def __init__(self, history_repo=None, parent=None):
        super().__init__(parent)
        self._repo = history_repo
        root = QVBoxLayout(self)
        tip = QLabel("最近已完成对局（只读本地 SQLite；无数据即空）")
        tip.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(tip)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "胜负", "时长", "英雄", "K/D/A", "位置"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)

    def refresh(self):
        if self._repo is None:
            self.table.setRowCount(0)
            return
        try:
            matches = self._repo.get_recent_matches(limit=20)
        except Exception as e:
            self.table.setRowCount(0)
            self.table.setColumnCount(1)
            return
        self.table.setRowCount(0)
        for m in matches:
            # 找本局中"自己"的玩家行
            self_p = None
            try:
                for p in self._repo.get_match_players(m["local_match_id"]):
                    if p.get("is_self"):
                        self_p = p
                        break
            except Exception:
                self_p = None
            row = self.table.rowCount()
            self.table.insertRow(row)
            vals = [
                _fmt_time(m.get("start_time")),
                m.get("result") or "UNKNOWN",
                _fmt_dur(m.get("duration")),
                (self_p or {}).get("champion_name") or "UNKNOWN",
                f"{(self_p or {}).get('kills',0)}/{(self_p or {}).get('deaths',0)}/{(self_p or {}).get('assists',0)}" if self_p else "UNKNOWN",
                (self_p or {}).get("position") or "UNKNOWN",
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 1:
                    it.setForeground(Qt.darkGreen if v == "Win" else Qt.red if v == "Lose" else Qt.gray)
                self.table.setItem(row, c, it)
