"""Lucide 线性图标工具（MIT 许可 SVG path，替代 Emoji / 系统图标混用）。

用法：
    from app.ui.widgets.icon_utils import lucide_icon
    btn.setIcon(lucide_icon("play", size=20, color=Tokens.TEXT_PRIMARY))

图标为线性描边风格（stroke），播放三角为填充（fill）；颜色在生成时固化，
disabled 态由 QIcon 自动灰色化。SVG path 数据来自 Lucide（ISC 许可，
https://lucide.dev）。
"""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

try:  # PySide6 完整安装含 QtSvg；缺失时降级为文字图标，不影响布局
    from PySide6.QtSvg import QSvgRenderer
    _SVG_OK = True
except Exception:  # pragma: no cover
    _SVG_OK = False

# name -> Lucide body（viewBox 24x24）
_LUCIDE = {
    "play": '<path d="m6 3 14 9-14 9V3z"/>',
    "pause": '<line x1="9" x2="9" y1="4" y2="20"/><line x1="15" x2="15" y1="4" y2="20"/>',
    "square": '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>',
    "skip-back": ('<polygon points="19 20 9 12 19 4 19 20"/>'
                  '<line x1="5" y1="19" x2="5" y2="5"/>'),
    "skip-forward": ('<polygon points="5 4 15 12 5 20 5 4"/>'
                     '<line x1="19" y1="5" x2="19" y2="19"/>'),
    "repeat": ('<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/>'
               '<path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>'),
    "trash": ('<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'
              '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
              '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>'),
    "music": ('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/>'
              '<circle cx="18" cy="16" r="3"/>'),
    "mic": ('<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>'
            '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>'),
    "book-open": ('<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>'
                  '<path d="M4 19.5a2.5 2.5 0 0 1 2.5-2.5H20"/>'),
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "maximize": ('<path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
                 '<path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
                 '<path d="M3 16v3a2 2 0 0 0 2 2h3"/>'
                 '<path d="M16 21h3a2 2 0 0 0 2-2v-3"/>'),
    "volume-2": ('<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/>'
                 '<path d="M16 9a5 5 0 0 1 0 6"/>'
                 '<path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>'),
    "volume-x": ('<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/>'
                 '<line x1="22" y1="9" x2="16" y2="15"/>'
                 '<line x1="16" y1="9" x2="22" y2="15"/>'),
    "rotate-ccw": ('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
                   '<path d="M3 3v5h5"/>'),
    "grid": ('<rect x="3" y="3" width="7" height="7"/>'
             '<rect x="14" y="3" width="7" height="7"/>'
             '<rect x="14" y="14" width="7" height="7"/>'
             '<rect x="3" y="14" width="7" height="7"/>'),
    "list": ('<line x1="8" y1="6" x2="21" y2="6"/>'
             '<line x1="8" y1="12" x2="21" y2="12"/>'
             '<line x1="8" y1="18" x2="21" y2="18"/>'
             '<line x1="3" y1="6" x2="3.01" y2="6"/>'
             '<line x1="3" y1="12" x2="3.01" y2="12"/>'
             '<line x1="3" y1="18" x2="3.01" y2="18"/>'),
    "link": ('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
             '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
    "cpu": ('<rect x="4" y="4" width="16" height="16" rx="2"/>'
            '<rect x="9" y="9" width="6" height="6"/>'
            '<path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/>'
            '<path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/>'
            '<path d="M9 2v2"/><path d="M9 20v2"/>'),
    "mic-off": ('<line x1="2" x2="22" y1="2" y2="22"/>'
                '<path d="M18.89 13.23A7.12 7.12 0 0 0 19 12v-2"/>'
                '<path d="M5 10v2a7 7 0 0 0 12 5"/>'
                '<path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/>'
                '<path d="M9 9v3a3 3 0 0 0 5.12 2.12"/>'
                '<line x1="12" y1="19" x2="12" y2="22"/>'),
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "minus": '<line x1="5" y1="12" x2="19" y2="12"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "settings-2": ('<path d="M20 7h-9"/><path d="M14 17H5"/>'
                   '<circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>'),
}

_FALLBACK_CHAR = {
    "play": "▶", "pause": "⏸", "square": "⏹", "skip-back": "⏮", "skip-forward": "⏭",
    "repeat": "↻", "trash": "🗑", "music": "♫", "mic": "🎤",
    "book-open": "📖", "search": "🔍", "maximize": "⛶",
    "volume-2": "🔊", "volume-x": "🔇", "rotate-ccw": "↺",
    "grid": "▦", "list": "☰",
    "link": "🔗", "cpu": "▦", "mic-off": "🎤",
    "x": "✕", "minus": "−", "plus": "＋", "chevron-down": "▾",
}


def lucide_icon(name: str, size: int = 20, color: str = "#e0e0e8",
                stroke_width: float = 2.0) -> QIcon:
    """生成指定颜色/尺寸的线性图标 QIcon（线程无关，主线程调用）。"""
    if not _SVG_OK:  # pragma: no cover
        return _fallback_icon(name, size, color)
    body = _LUCIDE.get(name)
    if body is None:  # pragma: no cover
        body = _LUCIDE["music"]
    fill = "currentColor" if name == "play" else "none"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="{fill}" stroke="currentColor" '
        f'stroke-width="{stroke_width}" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    ).replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return QIcon(pm)


def _fallback_icon(name: str, size: int, color: str) -> QIcon:  # pragma: no cover
    from PySide6.QtGui import QColor, QFont
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    f = QFont()
    f.setPixelSize(int(size * 0.8))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(pm.rect(), Qt.AlignCenter, _FALLBACK_CHAR.get(name, "♪"))
    p.end()
    return QIcon(pm)
