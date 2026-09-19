"""主界面 Tab 容器：4 个 Tab + 底部状态栏。"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QLabel

from app.ui.panels.automation_panel import AutomationPanel
from app.ui.panels.cooldown_panel import CooldownPanel
from app.ui.panels.current_game_panel import CurrentGamePanel
from app.ui.panels.history_panel import HistoryPanel
from app.ui.panels.music_panel import MusicPanel
from app.ui.panels.ongoing_panel import OngoingPanel
from app.ui.panels.status_bar import StatusBar


class MainTabs(QWidget):
    def __init__(self, automation_service, cooldown_manager, music,
                 ongoing_service=None, services_dict=None,
                 snapshot_provider=None, history_repo=None, parent=None):
        super().__init__(parent)
        print("[UI] MainTabs 创建中")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        tabs = QTabWidget()

        def _add(make, title):
            try:
                tabs.addTab(make(), title)
            except Exception as e:
                import traceback
                traceback.print_exc()
                tabs.addTab(QLabel(f"{title} 加载失败: {e}"), title)

        _add(lambda: CurrentGamePanel(snapshot_provider), "当前对局")
        _add(lambda: HistoryPanel(history_repo), "历史")
        _add(lambda: AutomationPanel(automation_service), "自动化")
        _add(lambda: OngoingPanel(ongoing_service), "对局信息")
        _add(lambda: CooldownPanel(cooldown_manager), "计时器")
        _add(lambda: MusicPanel(music), "音乐")
        layout.addWidget(tabs)

        try:
            status = StatusBar(services_dict or {
                "lcu": getattr(automation_service, "_lcu", None) if automation_service else None,
                "asr": None,
                "intent": None,
            })
            layout.addWidget(status)
        except Exception as e:
            print(f"[UI] StatusBar failed: {e}")

        print("[UI] MainTabs 完成")
