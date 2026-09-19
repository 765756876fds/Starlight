"""当前对局面板：复用 Runtime 的 GameSnapshot，UI 不直接轮询 2999。"""
from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


def _fmt_game_time(t: float) -> str:
    try:
        s = int(float(t))
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "UNKNOWN"


class CurrentGamePanel(QWidget):
    """只读当前局状态。数据全部来自 snapshot_provider()，不自己访问 2999。"""

    def __init__(self, snapshot_provider=None, parent=None):
        super().__init__(parent)
        self._provider = snapshot_provider
        root = QVBoxLayout(self)
        self.lbl = QLabel("未在对局中")
        self.lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.lbl)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def refresh(self):
        try:
            snap = self._provider() if self._provider else None
        except Exception:
            snap = None
        if snap is None:
            self.lbl.setText("未在对局中 / 暂无快照")
            return
        cp = getattr(snap, "current_player", None)
        lines = [f"<b>当前对局</b>　时间 {_fmt_game_time(getattr(snap, 'game_time', 0))}"]
        if cp is None:
            lines.append("玩家: UNKNOWN")
        else:
            lines.append(
                f"你: <b>{getattr(cp,'champion_name','?')}</b>　"
                f"Lv.{getattr(cp,'level','?')}　"
                f"{getattr(cp,'kills',0)}/{getattr(cp,'deaths',0)}/{getattr(cp,'assists',0)}　"
                f"{getattr(cp,'cs',0)} 刀"
            )
        team = getattr(cp, "team", "") if cp else ""
        lines.append(f"队伍: {team or 'UNKNOWN'}")
        gold = getattr(cp, "gold", 0) if cp else 0
        lines.append(f"经济: {gold:.0f}" if gold else "经济: UNKNOWN（2999 不提供双方金币）")
        allies = getattr(snap, "ally_champions", []) or []
        enemies = getattr(snap, "enemy_champions", []) or []
        lines.append(f"我方: {', '.join(allies) or 'UNKNOWN'}")
        lines.append(f"敌方: {', '.join(enemies) or 'UNKNOWN'}")
        self.lbl.setText("<br>".join(lines))
