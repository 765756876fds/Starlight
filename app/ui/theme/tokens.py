"""Glass 全局主题 Token（GlassMusicPlayer 风格 · 单一事实来源）。

排版阶梯：12 辅助字 / 14 正文 / 16 小标题 / 24 大标题。
间距网格：8px 基准（4 / 8 / 16 / 24 / 32）。
对比度：正文与辅助文字在深色底上均 ≥ WCAG AA 4.5:1
（TEXT_PRIMARY #e0e0e8 / TEXT_SECONDARY #a8a8c0，核算见 colors.py 注释）。
QSS 由 applier.build_glass_qss() 从本模块插值生成——禁止在 QSS 模板中
出现字面量颜色 / 字号 / 圆角 / 间距，一律取 Tokens。
"""
from .colors import Colors


def _rgb_of(hex_color: str) -> str:
    """'#rrggbb' → 'r, g, b'（供 rgba() 字符串插值，保持单一来源）。"""
    h = hex_color.lstrip("#")
    return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"


class Tokens:
    ON_ACCENT = Colors.ON_ACCENT
    SLIDER_HANDLE = Colors.ON_ACCENT
    SLIDER_HANDLE_HOVER = "#e0e0e0"
    # ---- 基础色（引用 colors.Colors，单一来源） ----
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

    # ---- RGB 派生（rgba() 插值用，全部来自 colors，无手写字面量） ----
    BG_DEEPEST_RGB = _rgb_of(Colors.BG_DEEPEST)       # 22, 22, 30
    BG_BASE_RGB = _rgb_of(Colors.BG_BASE)             # 30, 30, 46
    BG_ELEVATED_RGB = _rgb_of(Colors.BG_ELEVATED)     # 42, 42, 62
    BG_HOVER_RGB = _rgb_of(Colors.BG_HOVER)           # 51, 51, 74
    BG_ACTIVE_RGB = _rgb_of(Colors.BG_ACTIVE)         # 62, 62, 92
    BORDER_RGB = _rgb_of(Colors.BORDER)               # 58, 58, 78
    BORDER_LIGHT_RGB = _rgb_of(Colors.BORDER_LIGHT)   # 74, 74, 98
    TEXT_DISABLED_RGB = _rgb_of(Colors.TEXT_DISABLED)  # 90, 90, 114
    TEXT_PRIMARY_RGB = _rgb_of(Colors.TEXT_PRIMARY)     # 224, 224, 232
    TEXT_SECONDARY_RGB = _rgb_of(Colors.TEXT_SECONDARY)  # 168, 168, 192
    TEXT_RGB = TEXT_SECONDARY_RGB   # 进度条 groove 等“未激活”元素用次文字色
    SLIDER_GROOVE_RGB = "255, 255, 255"   # 滑条底槽（半透明白，alpha≈0.15 叠深色底=深灰细线）
    ACCENT_RGB = _rgb_of(Colors.ACCENT)               # 74, 158, 255
    ACCENT_LIGHT = Colors.ACCENT_HOVER                # 渐变亮端（进度条已播放区）

    # ---- 玻璃质感（半透明 / 卡片 / 高亮） ----
    GLASS_BG_RGB = BG_BASE_RGB
    GLASS_BG_ALPHA = 0.86                  # 主窗口不透明度
    GLASS_BG = f"rgba({GLASS_BG_RGB}, {int(255 * GLASS_BG_ALPHA)})"

    NAV_PILL_RGB = ACCENT_RGB              # 导航/选中胶囊高亮
    NAV_PILL_ALPHA = 0.24
    NAV_PILL = f"rgba({NAV_PILL_RGB}, {int(255 * NAV_PILL_ALPHA)})"
    NAV_PILL_BORDER = f"rgba({ACCENT_RGB}, 0.55)"

    CARD_RGB = BG_ELEVATED_RGB
    CARD_ALPHA = 0.74                  # 卡片常态不透明度（通透玻璃感）
    CARD_HOVER_ALPHA = 0.86            # hover 时略实（呼吸）
    CARD = f"rgba({CARD_RGB}, {int(255 * CARD_ALPHA)})"
    CARD_HOVER = Colors.BG_HOVER
    CARD_ACTIVE = Colors.BG_ACTIVE

    # 卡片渐变（顶部受光 → 底部渐沉，替代纯平底色，Glass 层次）
    CARD_TOP_RGB = BG_HOVER_RGB        # 渐变顶部（受光）
    CARD_BOTTOM_RGB = BG_ELEVATED_RGB  # 渐变底部
    CARD_HOVER_TOP_RGB = BG_HOVER_RGB
    CARD_HOVER_BOTTOM_RGB = BG_ACTIVE_RGB

    # 玻璃高光：顶部 1px 受光 / 底部 1px 暗线（极细层次，非粗重阴影）
    GLASS_TOP_LINE = "rgba(255, 255, 255, 26)"
    GLASS_TOP_LINE_HOVER = "rgba(255, 255, 255, 44)"
    GLASS_BOTTOM_LINE = "rgba(0, 0, 0, 60)"

    # 占位符文字（impeccable：placeholder 也在 WCAG 覆盖内）
    PLACEHOLDER = Colors.TEXT_SECONDARY   # #a8a8c0，对输入框底 6.0:1

    # 骨架微光扫描线（ui-ux-pro-max motion：#15 gradient sweep）
    SHIMMER_LINE = "rgba(255, 255, 255, 30)"

    # 无封面「动态专辑图」：按歌曲名哈希稳定选取的渐变对（Spotify/网易云式）。
    # 每对 = (顶部 RGB, 底部 RGB)——深色高级配色，随 seed 稳定取色。
    COVER_PALETTES = (
        ("99, 102, 241", "30, 27, 75"),     # 靛蓝 → 深蓝紫
        ("45, 212, 191", "15, 60, 66"),     # 青绿 → 墨青
        ("251, 146, 60", "88, 28, 12"),     # 暖橙 → 深棕
        ("236, 72, 153", "59, 7, 42"),      # 玫粉 → 酒红紫
        ("74, 222, 128", "10, 48, 26"),     # 荧光绿 → 墨绿
        ("96, 165, 250", "23, 37, 84"),     # 天蓝 → 深海蓝
    )
    # 专辑图中首字（浅色半透明白，impeccable：浅色字在深色渐变上保持可读）
    COVER_LETTER = "rgba(255, 255, 255, 170)"

    INPUT_RGB = BG_ELEVATED_RGB
    INPUT_ALPHA = 0.78
    INPUT_BG = f"rgba({INPUT_RGB}, {int(255 * INPUT_ALPHA)})"
    SCROLL_HANDLE = f"rgba({TEXT_DISABLED_RGB}, 150)"   # 极简细线半透明

    # ---- 圆角（克制化：10 全局 / 8 小控件 / 12 窗口） ----
    RADIUS = 10
    RADIUS_SMALL = 8
    WINDOW_RADIUS = 12

    # ---- 排版阶梯（12 / 14 / 16 / 24） ----
    FONT_FAMILY = "Microsoft YaHei UI, Microsoft YaHei, sans-serif"
    FONT_SIZE_SUB = 12      # 辅助字：元信息/歌手/状态
    FONT_SIZE_BODY = 14     # 正文：歌名/常规文字/按钮
    FONT_SIZE_TITLE = 16    # 小标题：当前曲目/区块标题
    FONT_SIZE_HERO = 24     # 大标题：启动页
    FONT_SIZE = FONT_SIZE_BODY   # 正文别名（兼容既有引用）

    # ---- 间距网格（8px 基准） ----
    SPACING_XS = 4
    SPACING_SM = 8
    SPACING_MD = 16
    SPACING_LG = 24
    SPACING_XL = 32

    # ---- 卡片（网格） ----
    CARD_WIDTH = 160
    CARD_HEIGHT = 200
    CARD_PADDING = 6                 # 卡片内边距（封面缩进）
    COVER_SIZE = CARD_WIDTH - 2 * CARD_PADDING   # 148（正方形封面）

    # ---- 高亮边框 ----
    BORDER_HIGHLIGHT = f"rgba({ACCENT_RGB}, 0.45)"   # hover 细高光
    BORDER_FOCUS = Colors.ACCENT                     # focus 发光轮廓

    # ---- 按钮尺寸 ----
    BTN_MAIN = 44          # 播放/暂停主按钮直径（高频操作，放大）
    BTN_SUB = 36           # 上一首/下一首


def refresh_color_tokens():
    """Refresh derived colors only; geometry, fonts and media artwork stay intact."""
    from .colors import COLOR_ROLES, current_theme
    for alias in COLOR_ROLES:
        setattr(Tokens, alias, getattr(Colors, alias))
        setattr(Tokens, alias + "_RGB", _rgb_of(getattr(Colors, alias)))
    T = Tokens
    light = current_theme() == "light"
    T.SLIDER_HANDLE = Colors.ACCENT_HOVER if light else Colors.ON_ACCENT
    T.SLIDER_HANDLE_HOVER = Colors.ACCENT_PRESSED if light else "#e0e0e0"
    T.WAVEFORM_COLOR = Colors.TEXT_SECONDARY if light else Colors.ON_ACCENT
    T.WAVEFORM_ALPHA = 100 if light else 34
    T.TEXT_RGB = T.TEXT_SECONDARY_RGB
    T.SLIDER_GROOVE_RGB = T.TEXT_SECONDARY_RGB if light else "255, 255, 255"
    T.ACCENT_LIGHT = Colors.ACCENT_HOVER
    T.GLASS_BG_RGB = T.BG_BASE_RGB
    T.GLASS_BG_ALPHA = 0.97 if light else 0.86
    T.GLASS_BG = f"rgba({T.GLASS_BG_RGB}, {int(255 * T.GLASS_BG_ALPHA)})"
    T.NAV_PILL_RGB = T.ACCENT_RGB
    T.NAV_PILL = f"rgba({T.ACCENT_RGB}, {int(255 * T.NAV_PILL_ALPHA)})"
    T.NAV_PILL_BORDER = f"rgba({T.ACCENT_RGB}, 0.55)"
    T.CARD_RGB = T.BG_ELEVATED_RGB
    T.CARD_ALPHA = 0.96 if light else 0.74
    T.CARD_HOVER_ALPHA = 0.98 if light else 0.86
    T.CARD = f"rgba({T.CARD_RGB}, {int(255 * T.CARD_ALPHA)})"
    T.CARD_HOVER, T.CARD_ACTIVE = Colors.BG_HOVER, Colors.BG_ACTIVE
    T.CARD_TOP_RGB, T.CARD_BOTTOM_RGB = T.BG_HOVER_RGB, T.BG_ELEVATED_RGB
    T.CARD_HOVER_TOP_RGB, T.CARD_HOVER_BOTTOM_RGB = T.BG_HOVER_RGB, T.BG_ACTIVE_RGB
    T.GLASS_TOP_LINE = "rgba(255, 255, 255, 120)" if light else "rgba(255, 255, 255, 26)"
    T.GLASS_TOP_LINE_HOVER = "rgba(255, 255, 255, 180)" if light else "rgba(255, 255, 255, 44)"
    T.GLASS_BOTTOM_LINE = "rgba(0, 0, 0, 20)" if light else "rgba(0, 0, 0, 60)"
    T.PLACEHOLDER = Colors.TEXT_SECONDARY
    T.INPUT_RGB, T.INPUT_ALPHA = T.BG_ELEVATED_RGB, 0.96 if light else 0.78
    T.INPUT_BG = f"rgba({T.INPUT_RGB}, {int(255 * T.INPUT_ALPHA)})"
    T.SCROLL_HANDLE = f"rgba({T.TEXT_DISABLED_RGB}, 150)"
    T.BORDER_HIGHLIGHT = f"rgba({T.ACCENT_RGB}, 0.45)"
    T.BORDER_FOCUS = Colors.ACCENT


refresh_color_tokens()
