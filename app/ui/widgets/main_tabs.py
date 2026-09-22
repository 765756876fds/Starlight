"""主界面 Tab 容器：4 个 Tab + 底部状态栏。"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QLabel

from app.ui.panels.automation_panel import AutomationPanel
from app.ui.panels.cooldown_panel import CooldownPanel
from app.ui.panels.current_game_panel import CurrentGamePanel
from app.ui.panels.music_panel import MusicPanel
from app.ui.panels.ongoing_panel import OngoingPanel


def _build_acct(ongoing_service, services_dict):
    try:
        lcu = None
        if isinstance(services_dict, dict):
            lcu = services_dict.get("lcu")
        if lcu is None and ongoing_service is not None:
            lcu = getattr(ongoing_service, "_lcu", None)
        if lcu is None or not hasattr(lcu, "get"):
            return None
        from app.lol.ongoing.account_history import AccountMatchHistoryService
        return AccountMatchHistoryService(lcu.get, lcu.post)
    except Exception:
        return None


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

        def _make_current_game():
            lcu = (services_dict or {}).get("lcu")
            lcu_get = lcu.get if lcu and hasattr(lcu, "get") else None
            lcu_post = lcu.post if lcu and hasattr(lcu, "post") else None
            return CurrentGamePanel(snapshot_provider, lcu_get=lcu_get, lcu_post=lcu_post)
        _add(_make_current_game, "当前对局")
        _add(lambda: AutomationPanel(automation_service, lcu=(services_dict or {}).get("lcu")), "自动化")
        _add(lambda: OngoingPanel(ongoing_service, account_history_service=_build_acct(ongoing_service, services_dict)), "对局信息")
        _add(lambda: CooldownPanel(cooldown_manager), "计时器")
        _add(lambda: MusicPanel(music), "音乐")
        layout.addWidget(tabs)

        print("[UI] MainTabs 完成")
