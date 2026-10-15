"""主界面 Tab 容器：5 个 Tab + 底部状态栏。"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QLabel

from app.ui.panels.automation_panel import AutomationPanel
from app.ui.panels.cooldown_panel import CooldownPanel
from app.ui.panels.current_game_panel import CurrentGamePanel
from app.ui.panels.movie_panel import MoviePanel
from app.ui.panels.music_panel import MusicPanel
from app.ui.panels.ongoing_panel import OngoingPanel
from app.ui.panels.settings_panel import SettingsPanel
from app.ui.settings_store import load_ui_prefs


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
                 snapshot_provider=None, history_repo=None,
                 combo_manager=None, tts=None, equip_toggle=None,
                 overlay_toggle=None, overlay_enabled: bool = True, parent=None):
        super().__init__(parent)
        print("[UI] MainTabs 创建中")
        self._music = music
        self._tts = tts
        self._equip_toggle = equip_toggle
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        tabs = QTabWidget()
        self._panels = {}

        def _add(make, title, attr=None):
            try:
                w = make()
                tabs.addTab(w, title)
                if attr:
                    self._panels[attr] = w
            except Exception as e:
                import traceback
                traceback.print_exc()
                tabs.addTab(QLabel(f"{title} 加载失败: {e}"), title)

        def _make_current_game():
            lcu = (services_dict or {}).get("lcu")
            lcu_get = lcu.get if lcu and hasattr(lcu, "get") else None
            lcu_post = lcu.post if lcu and hasattr(lcu, "post") else None
            return CurrentGamePanel(snapshot_provider, lcu_get=lcu_get, lcu_post=lcu_post, ongoing_service=ongoing_service)
        _add(_make_current_game, "当前对局", "current")
        _add(lambda: AutomationPanel(automation_service, lcu=(services_dict or {}).get("lcu"), combo_manager=combo_manager), "自动化", "automation")
        _add(lambda: OngoingPanel(ongoing_service, account_history_service=_build_acct(ongoing_service, services_dict)), "对局信息", "ongoing")
        # _add(lambda: CooldownPanel(cooldown_manager), "计时器")  # 隐藏计时器tab
        _add(lambda: MusicPanel(music), "音乐", "music")
        _add(lambda: MoviePanel(movie=None, music=self._music), "电影", "movie")
        _add(lambda: SettingsPanel(
                music=music, tts=tts, combo_manager=combo_manager,
                equip_toggle=equip_toggle, overlay_toggle=overlay_toggle,
                overlay_enabled=overlay_enabled,
                on_refresh_changed=self._apply_refresh_interval,
            ), "设置", "settings")
        layout.addWidget(tabs)

        # 应用持久化的刷新间隔到各面板 timer（settings.yaml ui.refresh_interval 原为死配置）
        self._apply_refresh_interval(int(load_ui_prefs().get("ui_refresh_interval", 500)))
        print("[UI] MainTabs 完成")

    def _apply_refresh_interval(self, ms: int) -> None:
        """把刷新间隔应用到各面板的 _timer（只改间隔，不动其他）。"""
        ms = max(100, int(ms))
        for name in ("current", "automation", "ongoing", "music"):
            p = self._panels.get(name)
            if p is None:
                continue
            t = getattr(p, "_timer", None)
            if t is not None:
                try:
                    t.setInterval(ms)
                except Exception:
                    pass
