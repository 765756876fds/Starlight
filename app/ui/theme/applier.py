"""Glass 全局主题（单一事实来源 = tokens.py）。

QSS 由 build_glass_qss() 在 Python 中插值 Tokens 生成；theme.qss 独立文件
已移除——任何颜色 / 字号 / 圆角 / 间距必须改 Tokens，禁止在模板中出现
字面量（防 colors.py 改动后 QSS 不同步的漂移）。
"""
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .tokens import Tokens

_ICONS_DIR = Path(__file__).parent / "icons"


def _qss_url(rel_name: str) -> str:
    """QSS url() 用绝对路径（Windows 反斜杠转正斜杠）。"""
    return (_ICONS_DIR / rel_name).as_posix()


def _load_qss(name: str) -> str:
    """仅旧 dark 主题使用（未 token 化的历史文件，保留兼容）。"""
    qss_path = Path(__file__).parent / name
    with open(qss_path, "r", encoding="utf-8") as f:
        return f.read()


def build_glass_qss() -> str:
    """从 Tokens 拼装 Glass 全局样式。返回的字符串可直接 setStyleSheet。"""
    T = Tokens
    return f"""/* Glass 全局样式（由 applier.build_glass_qss() 从 tokens.py 生成） */

/* 全局 */
QWidget {{
    background-color: {T.BG_BASE};
    color: {T.TEXT_PRIMARY};
    font-family: "{T.FONT_FAMILY}";
    font-size: {T.FONT_SIZE_BODY}px;
}}
QMainWindow {{ background: transparent; }}
QDialog {{
    background-color: {T.CARD};
    border: 1px solid rgba({T.ACCENT_RGB}, 0.25);
    border-radius: {T.RADIUS}px;
}}
QLabel {{ background: transparent; color: {T.TEXT_PRIMARY}; }}
QLabel[class="secondary"] {{ color: {T.TEXT_SECONDARY}; font-size: {T.FONT_SIZE_SUB}px; }}
QLabel[class="title"] {{ color: {T.TEXT_PRIMARY}; font-size: {T.FONT_SIZE_TITLE}px; font-weight: bold; }}
QLabel[class="status-success"] {{ color: {T.SUCCESS}; }}
QLabel[class="status-error"] {{ color: {T.DANGER}; }}
QLabel[class="status-warning"] {{ color: {T.WARNING}; }}

/* ---- 按钮：六态（default / hover / pressed(active) / focus / disabled / primary） ---- */
QPushButton {{
    background: transparent;
    color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210);
    border-radius: {T.RADIUS}px;
    padding: 8px 16px;
    min-height: 20px;
    font-size: {T.FONT_SIZE_BODY}px;
}}
/* hover：背景微亮 + 1px 细高光边框 */
QPushButton:hover {{ background-color: rgba({T.BG_HOVER_RGB}, 215); border-color: {T.BORDER_HIGHLIGHT}; }}
/* pressed（active）：加深 + 1px 下沉感 */
QPushButton:pressed {{ background-color: rgba({T.BG_ACTIVE_RGB}, 225); padding-top: 9px; padding-bottom: 7px; }}
/* focus：主色发光轮廓 + 背景微亮（键盘可达性） */
QPushButton:focus {{ border-color: {T.BORDER_FOCUS}; background-color: rgba({T.BG_HOVER_RGB}, 200); }}
/* disabled：半透明 + 灰字，不可点 */
QPushButton:disabled {{
    color: {T.TEXT_DISABLED};
    background-color: rgba({T.BG_DEEPEST_RGB}, 180);
    border-color: rgba({T.CARD_RGB}, 200);
}}
/* primary：主行动按钮（播放/搜索等高频） */
QPushButton[class="primary"] {{
    background-color: {T.ACCENT};
    border-color: {T.ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[class="primary"]:hover {{ background-color: {T.ACCENT_HOVER}; border-color: {T.ACCENT_HOVER}; }}
QPushButton[class="primary"]:pressed {{ background-color: {T.ACCENT_PRESSED}; padding-top: 9px; padding-bottom: 7px; }}
QPushButton[class="primary"]:focus {{ border-color: #ffffff; }}
QPushButton[class="primary"]:disabled {{
    background-color: rgba({T.ACCENT_RGB}, 120);
    border-color: transparent;
    color: rgba(255, 255, 255, 150);
}}

/* 播放控制金字塔：主按钮（播放/暂停）放大，跳曲次之，其余紧凑 */
QPushButton#btn_toggle {{
    min-width: {T.BTN_MAIN}px; min-height: {T.BTN_MAIN}px;
    border-radius: {int(T.BTN_MAIN / 2)}px; padding: 0;
    background-color: {T.ACCENT}; border: 1px solid {T.ACCENT};
}}
QPushButton#btn_toggle:hover {{ background-color: {T.ACCENT_HOVER}; border-color: {T.ACCENT_HOVER}; }}
QPushButton#btn_toggle:pressed {{ background-color: {T.ACCENT_PRESSED}; padding: 0; }}
QPushButton#btn_toggle:focus {{ border: 1px solid #ffffff; }}
QPushButton#btn_prev, QPushButton#btn_next, QPushButton#btn_stop {{
    min-width: {T.BTN_SUB}px; min-height: {T.BTN_SUB}px;
    border-radius: {int(T.BTN_SUB / 2)}px; padding: 0;
}}
QPushButton#btn_mode, QPushButton#btn_clear_queue, QPushButton#btn_highlight {{
    padding: 6px 12px; min-height: 18px;
}}

/* ToggleButton（高潮模式等开关） */
QPushButton[toggle="on"] {{ background-color: rgba({_rgb(T.SUCCESS)}, 90); border: 1px solid {T.SUCCESS}; color: {T.SUCCESS}; }}
QPushButton[toggle="on"]:hover {{ background-color: rgba({_rgb(T.SUCCESS)}, 130); }}
QPushButton[toggle="off"] {{ background-color: rgba({T.CARD_RGB}, 200); border: 1px solid rgba({T.BORDER_LIGHT_RGB}, 200); color: {T.TEXT_SECONDARY}; }}
QPushButton[toggle="off"]:hover {{ background-color: rgba({T.BG_HOVER_RGB}, 210); border-color: {T.BORDER_LIGHT}; }}

/* 通用 checkable（视图切换等原生 isChecked 按钮） */
QPushButton:checked {{ background-color: rgba({_rgb(T.ACCENT)}, 40); border: 1px solid {T.ACCENT}; color: {T.ACCENT}; }}
QPushButton:checked:hover {{ background-color: rgba({_rgb(T.ACCENT)}, 70); }}

/* Combo：序列步骤半透明圆角卡片 */
QWidget#combo_step_card {{
    background-color: rgba({T.CARD_RGB}, 110);
    border: 1px solid rgba({T.BORDER_RGB}, 90);
    border-radius: {T.RADIUS_SMALL}px;
}}
/* Combo：删除英雄（危险操作）深红半透明底 + 红字 */
QPushButton#btn_delete_champion {{
    background-color: rgba({_rgb(T.DANGER)}, 42);
    border: 1px solid rgba({_rgb(T.DANGER)}, 70);
    color: #f87171; font-weight: 600;
}}
QPushButton#btn_delete_champion:hover {{ background-color: rgba({_rgb(T.DANGER)}, 80); color: #fecaca; }}
QPushButton#btn_delete_champion:pressed {{ background-color: rgba({_rgb(T.DANGER)}, 110); }}

/* 输入 */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {T.INPUT_BG};
    color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210);
    border-radius: {T.RADIUS}px;
    padding: 6px 10px;
    selection-background-color: {T.ACCENT}; selection-color: #ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {T.ACCENT};
    background-color: rgba({T.BG_HOVER_RGB}, 200);
}}
QLineEdit::placeholder {{ color: {T.PLACEHOLDER}; }}

/* 下拉 */
QComboBox {{
    background-color: {T.INPUT_BG};
    color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210);
    border-radius: {T.RADIUS}px;
    padding: 6px 10px; min-height: 20px;
}}
QComboBox:hover {{ border-color: {T.ACCENT}; }}
QComboBox:focus {{ border-color: {T.ACCENT}; background-color: rgba({T.BG_HOVER_RGB}, 200); }}
QComboBox::drop-down {{ border: none; background: transparent; width: 26px; }}
QComboBox::down-arrow {{
    image: url("{_qss_url('chevron-down.svg')}");
    width: 16px; height: 16px; margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: rgba({T.CARD_RGB}, 240); color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210); border-radius: {T.RADIUS_SMALL}px;
    selection-background-color: {T.ACCENT}; selection-color: #ffffff; outline: none;
}}

/* 数字微调：隐藏原生上下按钮，输入框走 Glass 圆角样式 */
QSpinBox, QDoubleSpinBox {{
    background-color: {T.INPUT_BG};
    color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210);
    border-radius: {T.RADIUS}px;
    padding: 6px 10px; min-height: 20px;
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {T.ACCENT}; background-color: rgba({T.BG_HOVER_RGB}, 200); }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 0; height: 0; border: none; background: transparent;
}}

/* Tab：半透明面板 + 导航胶囊高亮 */
QTabWidget::pane {{
    border: 1px solid rgba({T.ACCENT_RGB}, 0.22);
    border-radius: {T.RADIUS}px;
    background-color: rgba({T.CARD_RGB}, 230);
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {T.TEXT_SECONDARY};
    padding: 9px 20px;
    border: none;
    border-radius: {T.RADIUS}px;
    font-weight: 500;
    margin: 2px 3px;
}}
QTabBar::tab:hover {{ color: {T.TEXT_PRIMARY}; background-color: rgba({T.BG_HOVER_RGB}, 180); }}
QTabBar::tab:focus {{ border: 1px solid {T.ACCENT}; }}
QTabBar::tab:selected {{
    color: {T.TEXT_PRIMARY};
    background-color: {T.NAV_PILL};
    border: 1px solid {T.NAV_PILL_BORDER};
}}

/* 列表 */
QListWidget, QListView {{
    background-color: rgba({T.BG_BASE_RGB}, 210);
    color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210);
    border-radius: {T.RADIUS}px;
    outline: none; padding: 4px;
}}
QListWidget:focus, QListView:focus {{ border-color: {T.ACCENT}; }}
/* 焦点虚线框地毯式歼灭：任何获得焦点的控件都走自定义 accent 微亮/细边框，
   绝不显示 Qt 原生 PE_FrameFocusRect 虚线（QSS 定义了 border 即由样式表接管绘制） */
QTableView:focus, QTreeView:focus {{ border-color: {T.ACCENT}; }}
QCheckBox:focus {{ color: {T.TEXT_PRIMARY}; }}
QCheckBox:focus::indicator {{ border-color: {T.ACCENT}; }}
QSlider:focus::handle {{ background: {T.ACCENT}; }}
QMenu:focus {{ border: 1px solid rgba({T.ACCENT_RGB}, 120); }}
QScrollArea:focus {{ border: none; }}
QTabWidget:focus, QTabWidget:focus QTabBar::tab {{ border: 1px solid {T.ACCENT}; }}
QListWidget::item {{ padding: 8px 10px; border-radius: {T.RADIUS_SMALL}px; margin: 2px 0; }}
QListWidget::item:hover {{ background-color: rgba({T.BG_ELEVATED_RGB}, 200); }}
QListWidget::item:selected {{ background-color: {T.NAV_PILL}; color: {T.TEXT_PRIMARY}; }}
/* 卡片网格列表：由 MusicCardDelegate 自绘（此处的 item 规则不作用于卡片） */

/* 滚动条：极简细线半透明（track/add-page/sub-page 全透明、无箭头，仅悬浮显示滑块） */
QScrollBar:vertical, QScrollBar:horizontal {{ background: transparent; margin: 0; }}
QScrollBar::track:vertical, QScrollBar::track:horizontal {{ background: transparent; border: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent; border: none; width: 0; height: 0;
}}
QScrollBar:vertical {{ width: 6px; }}
QScrollBar::handle:vertical {{ background: {T.SCROLL_HANDLE}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: rgba({T.ACCENT_RGB}, 150); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; width: 0; background: transparent; border: none; }}
QScrollBar:horizontal {{ height: 6px; }}
QScrollBar::handle:horizontal {{ background: {T.SCROLL_HANDLE}; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: rgba({T.ACCENT_RGB}, 150); }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; height: 0; background: transparent; border: none; }}

/* 状态栏（Glass）：半透明底 + 极细上边框，与主窗口融合 */
QStatusBar {{ background-color: rgba({T.CARD_RGB}, 60); border-top: 1px solid rgba({T.BORDER_RGB}, 120); color: {T.TEXT_SECONDARY}; }}
QStatusBar::item {{ border: none; }}
QLabel[class="status_item"] {{ padding: 0 {T.SPACING_SM}px; }}
QPushButton#btn_status_mic {{
    background-color: rgba({T.CARD_RGB}, 210); border: 1px solid rgba({T.BORDER_RGB}, 200);
    border-radius: 14px; padding: 2px {T.SPACING_SM}px; color: {T.TEXT_SECONDARY};
}}
QPushButton#btn_status_mic:hover {{ background-color: rgba({T.BG_HOVER_RGB}, 220); border-color: {T.BORDER_LIGHT}; }}
QPushButton#btn_status_mic:pressed {{ background-color: rgba({T.BG_ACTIVE_RGB}, 230); padding-top: 3px; padding-bottom: 1px; }}
/* 去 focus 粗框：聚焦仅微亮（视觉等同 hover），不出现蓝框 */
QPushButton#btn_status_mic:focus {{ border-color: rgba({T.BORDER_RGB}, 200); background-color: rgba({T.BG_HOVER_RGB}, 180); }}

/* Checkbox */
QCheckBox {{ color: {T.TEXT_PRIMARY}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border-radius: 6px;
    border: 1px solid rgba({T.BORDER_RGB}, 210); background-color: rgba({T.CARD_RGB}, 200);
}}
QCheckBox::indicator:hover {{ border-color: {T.ACCENT}; }}
QCheckBox::indicator:checked {{
    background-color: {T.ACCENT}; border-color: {T.ACCENT}; image: none;
}}

/* Slider：左亮蓝（已走过）/ 右半透明深灰（未走过）/ 纯白圆点 handle。
   子元素 background 用 transparent 在 Qt QSS 上不可靠（会露出原生亮色 groove），
   因此 add-page 必须显式声明深灰背景（与 groove 同色），不能依赖 transparent。 */
QSlider::groove:horizontal {{
    background: rgba({T.TEXT_RGB}, 38);   /* 底槽半透明深灰 */
    height: 6px; border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {T.ACCENT};               /* 已走过：亮蓝 */
    border-radius: 3px;
}}
QSlider::add-page:horizontal {{
    background: rgba({T.TEXT_RGB}, 38);   /* 未走过：显式深灰（同 groove） */
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: #ffffff;                   /* 纯白圆点 */
    width: 14px; height: 14px; margin: -4px 0; border-radius: 7px;
    border: 1px solid rgba({T.ACCENT_RGB}, 200);
}}
QSlider::handle:horizontal:hover {{
    background: #ffffff; border: 1px solid {T.ACCENT};
}}
QSlider:focus::handle {{
    background: #ffffff; border: 1px solid {T.ACCENT};
}}

/* GroupBox */
QGroupBox {{
    background-color: rgba({T.CARD_RGB}, 210);
    border: 1px solid rgba({T.BORDER_RGB}, 210);
    border-radius: {T.RADIUS}px; margin-top: 12px; padding-top: 12px; font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {T.TEXT_SECONDARY};
}}

/* Tooltip */
QToolTip {{
    background-color: rgba({T.CARD_RGB}, 245); color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.ACCENT_RGB}, 0.6); border-radius: {T.RADIUS_SMALL}px; padding: 6px 10px;
}}

/* Menu */
QMenu {{
    background-color: rgba({T.CARD_RGB}, 245); color: {T.TEXT_PRIMARY};
    border: 1px solid rgba({T.BORDER_RGB}, 210); border-radius: {T.RADIUS}px; padding: 6px;
}}
QMenu::item {{ padding: 8px 24px 8px 12px; border-radius: {T.RADIUS_SMALL}px; }}
QMenu::item:selected {{ background-color: {T.NAV_PILL}; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: rgba({T.BORDER_RGB}, 210); margin: 4px 8px; }}

/* ---- Glass 专属（主窗口外壳） ---- */
#GlassShellRoot {{ background: transparent; }}
/* GlassDialog 外壳：深色半透明圆角容器（无边框对话框共用） */
QWidget#GlassDialogShell {{
    background-color: rgba({T.CARD_RGB}, 246);
    border: 1px solid rgba({T.ACCENT_RGB}, 0.28);
    border-radius: {T.RADIUS}px;
}}
#GlassDialogShell QPushButton {{
    padding: 4px 10px; min-height: 0; min-width: 0;
}}
#GlassTitleBar {{
    background: transparent;
    border-top-left-radius: {T.WINDOW_RADIUS}px;
    border-top-right-radius: {T.WINDOW_RADIUS}px;
}}
#GlassTitleBar QLabel {{ color: {T.TEXT_SECONDARY}; font-size: {T.FONT_SIZE_SUB}px; }}
#GlassTitleBar QPushButton {{
    background: transparent; border: none; border-radius: {T.RADIUS_SMALL}px;
    padding: 4px 10px; min-height: 0; font-size: {T.FONT_SIZE_BODY}px; color: {T.TEXT_SECONDARY};
}}
#GlassTitleBar QPushButton:hover {{ background-color: rgba({T.BG_HOVER_RGB}, 200); color: {T.TEXT_PRIMARY}; }}
#GlassTitleBar QPushButton#btn_close:hover {{ background-color: {T.DANGER}; color: #ffffff; }}
QPushButton[class="btn-gear"] {{
    background: rgba({T.BG_HOVER_RGB}, 130); border: 1px solid rgba({T.BORDER_RGB}, 180);
    border-radius: 8px; padding: 0; min-height: 0; min-width: 0;
}}
QPushButton[class="btn-gear"]:hover {{ background: rgba({T.BG_HOVER_RGB}, 210); border-color: {T.ACCENT}; }}
QPushButton[class="btn-gear"]:pressed {{ background: rgba({T.ACCENT_RGB}, 90); border-color: {T.ACCENT}; }}
QPushButton[class="btn-gear"]:focus {{ border-color: rgba({T.ACCENT_RGB}, 180); }}
QPushButton[class="danger"] {{
    background-color: rgba({_rgb(T.DANGER)}, 90); color: #fecaca;
    border: 1px solid {T.DANGER}; border-radius: {T.RADIUS}px;
    padding: 6px 14px;
}}
QPushButton[class="danger"]:hover {{ background-color: {T.DANGER}; color: #ffffff; }}
QPushButton[class="danger"]:pressed {{ background-color: rgba({_rgb(T.DANGER)}, 70); }}
"""


def _rgb(hex_color: str) -> str:
    """供模板内 rgba() 用：'#rrggbb' → 'r, g, b'（从 Tokens/Colors 引用）。"""
    h = hex_color.lstrip("#")
    return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"


def apply_dark_theme(app: QApplication) -> None:
    """旧 dark 主题（历史文件，保留兼容；正常路径会被 Glass 覆盖）。"""
    app.setStyleSheet(_load_qss("dark.qss"))


def apply_glass_theme(app: QApplication) -> None:
    """Glass 全局样式（覆盖 dark 主题；MainWindow.__init__ 调用）。

    Fusion 基底：Windows 原生 style 会忽略 QSlider::groove/add-page 等子元素的
    QSS background（只画 sub-page，导致未走过区露出原生亮白/全蓝混淆），
    Fusion 对样式表子元素支持完整，保证「左亮蓝 / 右半透明深灰 / 白点」分层可靠。
    """
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setStyleSheet(build_glass_qss())
