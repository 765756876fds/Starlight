"""Glass 全局主题 Token（GlassMusicPlayer 风格 · 第一阶段）。

单一事实来源：颜色引用自 colors.Colors（既有深色板），本模块追加
玻璃质感相关 Token（半透明背景 / 卡片 / 高亮胶囊 / 圆角 / 字体 / 透明度）。
"""
from .colors import Colors


class Tokens:
    # ---- 基础色（引用既有色板，保持单一来源） ----
    BG_DEEPEST = Colors.BG_DEEPEST        # 窗口最深底
    BG_BASE = Colors.BG_BASE              # 常规底
    BG_ELEVATED = Colors.BG_ELEVATED      # 卡片/输入框底
    BG_HOVER = Colors.BG_HOVER
    BG_ACTIVE = Colors.BG_ACTIVE
    BORDER = Colors.BORDER
    BORDER_LIGHT = Colors.BORDER_LIGHT
    TEXT_PRIMARY = Colors.TEXT_PRIMARY
    TEXT_SECONDARY = Colors.TEXT_SECONDARY
    TEXT_DISABLED = Colors.TEXT_DISABLED
    ACCENT = Colors.ACCENT
    ACCENT_HOVER = Colors.ACCENT_HOVER
    ACCENT_PRESSED = Colors.ACCENT_PRESSED
    SUCCESS = Colors.SUCCESS
    WARNING = Colors.WARNING
    DANGER = Colors.DANGER
    INFO = Colors.INFO

    # ---- 玻璃质感（半透明 / 卡片 / 高亮） ----
    # 主窗口底色：深色半透明（配合 WA_TranslucentBackground + paintEvent 圆角）
    GLASS_BG_RGB = "30, 30, 46"            # = BG_BASE
    GLASS_BG_ALPHA = 0.86                  # 主窗口不透明度
    GLASS_BG = f"rgba({GLASS_BG_RGB}, {int(255 * GLASS_BG_ALPHA)})"

    # 导航胶囊高亮（半透明强调块，类似 GlassMusicPlayer 左侧高亮胶囊）
    NAV_PILL_RGB = "74, 158, 255"          # = ACCENT
    NAV_PILL_ALPHA = 0.24
    NAV_PILL = f"rgba({NAV_PILL_RGB}, {int(255 * NAV_PILL_ALPHA)})"
    NAV_PILL_BORDER = "rgba(74, 158, 255, 0.55)"

    # 卡片底色（面板/弹窗）
    CARD_RGB = "42, 42, 62"
    CARD_ALPHA = 0.92
    CARD = f"rgba({CARD_RGB}, {int(255 * CARD_ALPHA)})"
    CARD_HOVER = Colors.BG_HOVER
    CARD_ACTIVE = Colors.BG_ACTIVE

    # 输入框/滚动条
    INPUT_RGB = "42, 42, 62"
    INPUT_ALPHA = 0.78
    INPUT_BG = f"rgba({INPUT_RGB}, {int(255 * INPUT_ALPHA)})"
    SCROLL_HANDLE = "rgba(90, 90, 114, 150)"   # 极简细线半透明

    # ---- 圆角 / 字体 ----
    RADIUS = 12          # 全局圆角（按钮/输入/卡片）
    RADIUS_SMALL = 8     # 小控件
    WINDOW_RADIUS = 16   # 主窗口外框圆角
    FONT_FAMILY = "Microsoft YaHei UI, Microsoft YaHei, sans-serif"
    FONT_SIZE = 13
