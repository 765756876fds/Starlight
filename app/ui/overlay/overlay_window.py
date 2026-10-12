"""OverlayWindow：Qt 透明无边框置顶浮窗（敌方五人 CD + 可点击召唤师技能）。

H.4.4-R2 修正：
- 真实英雄头像（Data Dragon）
- 真实召唤师技能图标（Data Dragon）
- 分层半透明 HUD 风格
- 窗口位置自动适配屏幕
- 召唤师技能可点击，触发 CommandBus → CooldownManager
"""
from __future__ import annotations

import sys
from app.ui.theme.colors import Colors

from PySide6.QtCore import Qt, QTimer, Signal, QSize, QSettings
from PySide6.QtGui import QFont, QPixmap, QPainter, QPainterPath, QColor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QFrame,
)

from .overlay_model import OverlayModel, OverlaySpell, OverlaySkill, fmt_remaining
from .overlay_assets import get_champion_portrait, get_summoner_spell_icon_by_sid, get_summoner_spell_icon_by_zh
from ...lol.data.champion_catalog import ChampionCatalog
from ..settings_store import load_ui_prefs

# 全局 ChampionCatalog 实例
_champion_catalog = ChampionCatalog()


class ClickableSpellIcon(QFrame):
    """可点击的召唤师技能图标。"""
    clicked = Signal(str, str)  # (champion_name, spell_name)

    def __init__(self, champion_name: str, spell: OverlaySpell, parent=None, size: int = 48):
        super().__init__(parent)
        self._champion_name = champion_name
        self._spell = spell
        self._size = size
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(size, size)
        
        # 加载图标（优先用 sid，其次用中文名）
        # print(f"[Spell Icon] champion={champion_name}, spell_id={spell.spell_id}, spell_name={spell.spell_name}")
        if spell.spell_id > 0:
            self._pixmap = get_summoner_spell_icon_by_sid(spell.spell_id)
        else:
            self._pixmap = get_summoner_spell_icon_by_zh(spell.spell_name)
        if self._pixmap is None:
            print(f"[Spell Icon] 加载失败: spell_id={spell.spell_id}, spell_name={spell.spell_name}")
        self._update_style()

    def _update_style(self):
        """根据技能状态更新样式。"""
        if self._spell.active:
            # CD 中：红色边框
            self.setStyleSheet(f"""
                QFrame {{
                    border: 2px solid {Colors.DANGER};
                    border-radius: 4px;
                    background: transparent;
                }}
            """)
        else:
            # Ready：正常状态 - 无背景无边框，直接显示图标
            self.setStyleSheet("""
                QFrame {
                    border: none;
                    background: transparent;
                }
            """)

    def paintEvent(self, event):
        """绘制图标。"""
        super().paintEvent(event)
        if self._pixmap and not self._pixmap.isNull():
            painter = QPainter(self)
            painter.setOpacity(0.3)  # 图标 30% 透明
            painter.drawPixmap(self.rect(), self._pixmap)
            
            # 如果在 CD 中，绘制剩余时间（白色大字体，覆盖整个图标）
            if self._spell.active:
                # 半透明黑色背景覆盖整个图标
                painter.setOpacity(0.5)
                painter.fillRect(self.rect(), QColor(0, 0, 0))
                # 白色大数字居中
                painter.setOpacity(1.0)
                painter.setPen(QColor(255, 255, 255))
                painter.setFont(QFont("Microsoft YaHei", max(8, int(14 * self._size / 48)), QFont.Bold))
                txt = fmt_remaining(self._spell.remaining)
                painter.drawText(self.rect(), Qt.AlignCenter, txt)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            spell_name = self._spell.spell_name
            if spell_name and spell_name != "—":
                self.clicked.emit(self._champion_name, spell_name)
                # print(f"[Overlay Click] champion={self._champion_name}, spell={spell_name}")
        super().mousePressEvent(event)


class ChampionAvatar(QLabel):
    """英雄头像（Data Dragon 真实头像），支持死亡状态显示。"""
    def __init__(self, name_en: str, parent=None, size: int = 62):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)  # 130% 放大：48 → 62
        self.setAlignment(Qt.AlignCenter)
        self._name_en = name_en
        self._is_dead = False
        self._respawn_timer = 0.0
        
        # 加载头像
        self._pixmap = get_champion_portrait(name_en)
        # 头像 50% 透明
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        self._opacity_effect = QGraphicsOpacityEffect()
        self._opacity_effect.setOpacity(0.3)
        self.setGraphicsEffect(self._opacity_effect)
        self._update_display()

    def set_dead(self, is_dead: bool, respawn_timer: float = 0.0):
        """设置死亡状态。"""
        self._is_dead = is_dead
        self._respawn_timer = respawn_timer
        self._update_display()

    def _update_display(self):
        """更新显示。"""
        _s = self._size
        if self._is_dead and self._respawn_timer > 0:
            # 死亡状态：半透明头像 + 中央大号白色倒计时
            if self._pixmap and not self._pixmap.isNull():
                self.setPixmap(self._pixmap.scaled(_s, _s, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            self.setStyleSheet(f"""
                QLabel {{
                    border-radius: {int(24 * _s / 62)}px;
                    border: 2px solid rgba(255,255,255,0.3);
                    background-color: rgba(0, 0, 0, 0.5);
                    color: white;
                    font-weight: bold;
                    font-size: {int(16 * _s / 62)}px;
                }}
            """)
            self.setText(str(int(self._respawn_timer)))
        else:
            # 正常状态
            if self._pixmap and not self._pixmap.isNull():
                self.setPixmap(self._pixmap.scaled(_s, _s, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                self.setText("")
                self.setStyleSheet(f"""
                    QLabel {{
                        border-radius: {int(24 * _s / 62)}px;
                        border: 2px solid rgba(255,255,255,0.3);
                    }}
                """)
            else:
                # 占位：首字母
                self.setText(self._name_en[0] if self._name_en else "?")
                self.setStyleSheet(f"""
                    QLabel {{
                        background-color: {Colors.ACCENT};
                        color: white;
                        border-radius: {int(24 * _s / 62)}px;
                        font-weight: bold;
                        font-size: {int(18 * _s / 62)}px;
                        border: 2px solid rgba(255,255,255,0.3);
                    }}
                """)
                self.setFont(QFont("Microsoft YaHei", max(8, int(12 * _s / 62)), QFont.Bold))


class OverlayWindow(QWidget):
    # 点击信号：(champion_name, spell_name)
    spell_clicked = Signal(str, str)

    def __init__(self, model: OverlayModel, parent=None):
        super().__init__(parent)
        self._model = model
        self._drag_offset = None
        try:
            self._scale = float(load_ui_prefs().get("overlay_scale", 1.0))
        except Exception:
            self._scale = 1.0
        if self._scale <= 0 or self._scale > 3:
            self._scale = 1.0

        # 窗口属性
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(8, 8, 8, 8)
        self._root.setSpacing(6)

        # 100ms 刷新定时器
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(100)

        # 2s 低频检查 ui_prefs 的缩放变化（设置页改动后自动生效，不依赖 main 接线）
        self._scale_check = QTimer(self)
        self._scale_check.timeout.connect(self._check_scale)
        self._scale_check.start(2000)

        self._place_default()
        self._apply_ws_ex_layered()
        self.hide()

    def _apply_ws_ex_layered(self) -> None:
        """保留 WS_EX_LAYERED（透明背景需要）。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            user32 = ctypes.windll.user32
            cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, cur | WS_EX_LAYERED)
        except Exception as e:
            print(f"[overlay] WS_EX_LAYERED 设置失败: {e}")

    def _place_default(self) -> None:
        """自动计算窗口位置，确保完整落在屏幕内。优先恢复上次拖动位置。"""
        screen = QApplication.primaryScreen().availableGeometry()
        # 窗口尺寸：宽度按缩放系数，高度自适应
        w = int(380 * self._scale)
        h = 300
        try:
            pos = QSettings("LoLMusicAgent", "Overlay").value("pos")
        except Exception:
            pos = None
        if pos is not None:
            x = int(pos.x())
            y = int(pos.y())
            # 限制在屏幕可见区域内（防止拖出屏后找不回）
            x = max(screen.x(), min(x, screen.x() + screen.width() - w))
            y = max(screen.y(), min(y, screen.y() + screen.height() - 40))
        else:
            # 默认右上角（右侧留 2px，顶部留 80px）
            x = screen.width() - w - 2
            y = 80
            if x + w > screen.width():
                x = screen.width() - w - 2
            if y + h > screen.height():
                y = screen.height() - h - 10
        self.setGeometry(x, y, w, h)

    def set_scale(self, scale: float) -> None:
        """设置缩放系数（紧凑 0.8 / 标准 1.0 / 大 1.2），立即重定位。"""
        scale = float(scale)
        if scale <= 0 or scale > 3:
            scale = 1.0
        if abs(scale - self._scale) < 1e-6:
            return
        self._scale = scale
        self._place_default()

    def _check_scale(self) -> None:
        """低频检查 ui_prefs 缩放设置变化（设置页改后自动生效）。"""
        try:
            s = float(load_ui_prefs().get("overlay_scale", 1.0))
        except Exception:
            return
        if s <= 0 or s > 3:
            s = 1.0
        if abs(s - self._scale) > 1e-6:
            self._scale = s
            self._place_default()

    # ---- 拖动：按下空白/头像区域拖动窗口；技能图标点击仍由子控件消费 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            try:
                QSettings("LoLMusicAgent", "Overlay").setValue("pos", self.pos())
            except Exception:
                pass
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _clear(self):
        while self._root.count():
            it = self._root.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _skill_dot(self, sk: OverlaySkill) -> QWidget:
        """Q/W/E/R 技能图标（用字母代替真实图标）。"""
        w = QFrame()
        _sz = int(32 * self._scale)
        w.setFixedSize(_sz, _sz)
        w.setFrameShape(QFrame.StyledPanel)
        
        letter = sk.skill.upper() if sk.skill else "?"
        
        if sk.active:
            color = Colors.WARNING if sk.remaining < 10 else Colors.WARNING_LIGHT
            txt = fmt_remaining(sk.remaining)
            w.setStyleSheet(f"""
                QFrame {{
                    background-color: rgba({int(int(color[1:3],16))},{int(int(color[3:5],16))},{int(int(color[5:7],16))},200);
                    border: 1px solid rgba(255,255,255,0.3);
                    border-radius: 4px;
                }}
            """)
            layout = QVBoxLayout(w)
            layout.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(txt)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: black; background: transparent; border: none; font-weight: bold;")
            lbl.setFont(QFont("Microsoft YaHei", max(6, int(7 * self._scale)), QFont.Bold))
            layout.addWidget(lbl)
        else:
            w.setStyleSheet(f"""
                QFrame {{
                    background-color: rgba(76, 175, 80, 150);
                    border: 1px solid rgba(255,255,255,0.2);
                    border-radius: 4px;
                }}
            """)
            layout = QVBoxLayout(w)
            layout.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(letter)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: white; background: transparent; border: none; font-weight: bold;")
            lbl.setFont(QFont("Microsoft YaHei", max(8, int(10 * self._scale)), QFont.Bold))
            layout.addWidget(lbl)
        
        return w

    def _build_enemy_card(self, row) -> QWidget:
        """构建单个敌方英雄卡片。
        
        布局：召唤师技能 → 头像（头像在最右）
        H.4.4-R13: 删除 Q/W/E/R，添加死亡状态显示
        """
        card = QFrame()
        card.setStyleSheet("""
            QFrame {
                background-color: transparent;
                border: none;
            }
        """)
        
        layout = QHBoxLayout(card)
        layout.setContentsMargins(int(142 * self._scale), 2, 2, 2)  # 左边距随缩放
        layout.setSpacing(int(14 * self._scale))  # 视觉间隔 14px ≈ 图标尺寸的 30%
        
        # 召唤师技能图标
        spell1_icon = ClickableSpellIcon(row.champion_name, row.spell1, size=int(48 * self._scale))
        spell1_icon.clicked.connect(self.spell_clicked.emit)
        layout.addWidget(spell1_icon)
        spell2_icon = ClickableSpellIcon(row.champion_name, row.spell2, size=int(48 * self._scale))
        spell2_icon.clicked.connect(self.spell_clicked.emit)
        layout.addWidget(spell2_icon)
        
        # 最右边：英雄头像（支持死亡状态）
        champ = _champion_catalog.get_by_id(row.champion_id) or _champion_catalog.get_by_name(row.champion_name)
        name_en = champ.name_en if champ else row.champion_name
        avatar = ChampionAvatar(name_en, size=int(62 * self._scale))
        avatar.set_dead(row.is_dead, row.respawn_timer)
        layout.addWidget(avatar)
        
        # 把剩余空间挤到右边，避免 layout 平均分配间隔
        layout.addStretch()
        
        return card

    def _refresh(self) -> None:
        try:
            rows = self._model.get_rows()
        except Exception:
            import traceback
            traceback.print_exc()
            rows = []
        
        self._clear()
        
        if not rows:
            if self.isVisible():
                self.hide()
            return
        
        for row in rows:
            card = self._build_enemy_card(row)
            self._root.addWidget(card)
        
        if not self.isVisible():
            self.show()


