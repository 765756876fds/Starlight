"""播放界面线性图标（S6，UI 对标 B站——同风格不抄版权）。

图标数据来源：Feather / Lucide（MIT / ISC 开源线性图标集）——stroke 线性、
24x24 viewBox、stroke-width 2、圆角端点（B站同风格但非其自研资源）。
QIcon 直接渲染 SVG（QtSvg 插件，PySide6 自带）。

按钮只换图标（setIcon），文本为空；tooltip 负责可读性（S5 已有惯例）。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

# Feather/Lucide path 数据（MIT/ISC 开源；linear stroke 风格统一）
_ICONS = {
    "play": '<path d="M8 5v14l11-7z"/>',
    "pause": '<line x1="10" y1="5" x2="10" y2="19"/>'
             '<line x1="14" y1="5" x2="14" y2="19"/>',
    "skip_forward": '<polygon points="5 4 15 12 5 20 5 4"/>'
                    '<line x1="19" y1="5" x2="19" y2="19"/>',
    "volume_high": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
                   '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>'
                   '<path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    "volume_low": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
                  '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>',
    "volume_muted": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
                    '<line x1="23" y1="9" x2="17" y2="15"/>'
                    '<line x1="17" y1="9" x2="23" y2="15"/>',
    "captions": '<path d="M19 6H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h14a2 2 0 0 0 '
                '2-2V8a2 2 0 0 0-2-2z"/>'
                '<path d="M10 10H7a1 1 0 0 0-1 1v2a1 1 0 0 0 1 1h3"/>'
                '<path d="M17 10h-3a1 1 0 0 0-1 1v2a1 1 0 0 0 1 1h3"/>',
    "maximize": '<path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3'
                'm0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>',
    "minimize": '<path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3'
                'm0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/>',
}

_CACHE = {}


def _render(name: str, size: int = 20, color: str = "#ddd") -> QPixmap:
    """SVG 字符串 → 指定尺寸/颜色 pixmap（stroke=currentColor 由 fill 参数替换）。
    QSvgRenderer 必须存在 QApplication（无 app 时崩 0xC0000409）——防御兜底。"""
    if QApplication.instance() is None:
        QApplication([])
    body = _ICONS[name]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode()
    renderer = QSvgRenderer()
    if not renderer.load(svg):
        return QPixmap(size, size)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return pm


def icon(name: str, color: str = "#ddd") -> QIcon:
    """线性图标 QIcon（带缓存；hover 高亮 = 换亮色版本）。"""
    key = (name, color)
    if key not in _CACHE:
        _CACHE[key] = QIcon(_render(name, color=color))
    return _CACHE[key]
