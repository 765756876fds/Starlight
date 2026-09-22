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
from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtGui import QFont, QPixmap, QPainter, QPainterPath, QColor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QFrame,
)

from .overlay_model import OverlayModel, OverlaySpell, OverlaySkill, fmt_remaining
from .overlay_assets import get_champion_portrait, get_summoner_spell_icon_by_sid, get_summoner_spell_icon_by_zh
from ...lol.data.champion_catalog import ChampionCatalog

# 全局 ChampionCatalog 实例
_champion_catalog = ChampionCatalog()


class ClickableSpellIcon(QFrame):
    """可点击的召唤师技能图标。"""
    clicked = Signal(str, str)  # (champion_name, spell_name)

    def __init__(self, champion_name: str, spell: OverlaySpell, parent=None):
        super().__init__(parent)
        self._champion_name = champion_name
        self._spell = spell
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(48, 48)
        
        # 加载图标（优先用 sid，其次用中文名）
        print(f"[Spell Icon] champion={champion_name}, spell_id={spell.spell_id}, spell_name={spell.spell_name}")
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
            self.setStyleSheet("""
                QFrame {
                    border: 2px solid #ff5252;
                    border-radius: 4px;
                    background: transparent;
                }
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
            painter.setOpacity(0.85)  # 图标半透明
            painter.drawPixmap(self.rect(), self._pixmap)
            
            # 如果在 CD 中，绘制剩余时间（白色大字体，覆盖整个图标）
            if self._spell.active:
                # 半透明黑色背景覆盖整个图标
                painter.setOpacity(0.5)
                painter.fillRect(self.rect(), QColor(0, 0, 0))
                # 白色大数字居中
                painter.setOpacity(1.0)
                painter.setPen(QColor(255, 255, 255))
                painter.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
                txt = fmt_remaining(self._spell.remaining)
                painter.drawText(self.rect(), Qt.AlignCenter, txt)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            spell_name = self._spell.spell_name
            if spell_name and spell_name != "—":
                self.clicked.emit(self._champion_name, spell_name)
                print(f"[Overlay Click] champion={self._champion_name}, spell={spell_name}")
        super().mousePressEvent(event)


class ChampionAvatar(QLabel):
    """英雄头像（Data Dragon 真实头像），支持死亡状态显示。"""
    def __init__(self, name_en: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(48, 48)
        self.setAlignment(Qt.AlignCenter)
        self._name_en = name_en
        self._is_dead = False
        self._respawn_timer = 0.0
        
        # 加载头像
        self._pixmap = get_champion_portrait(name_en)
        self._update_display()

    def set_dead(self, is_dead: bool, respawn_timer: float = 0.0):
        """设置死亡状态。"""
        self._is_dead = is_dead
        self._respawn_timer = respawn_timer
        self._update_display()

    def _update_display(self):
        """更新显示。"""
        if self._is_dead and self._respawn_timer > 0:
            # 死亡状态：半透明头像 + 中央大号白色倒计时
            if self._pixmap and not self._pixmap.isNull():
                self.setPixmap(self._pixmap.scaled(48, 48, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            self.setStyleSheet("""
                QLabel {
                    border-radius: 24px;
                    border: 2px solid rgba(255,255,255,0.3);
                    background-color: rgba(0, 0, 0, 0.5);
                    color: white;
                    font-weight: bold;
                    font-size: 16px;
                }
            """)
            self.setText(str(int(self._respawn_timer)))
        else:
            # 正常状态
            if self._pixmap and not self._pixmap.isNull():
                self.setPixmap(self._pixmap.scaled(48, 48, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                self.setText("")
                self.setStyleSheet("""
                    QLabel {
                        border-radius: 24px;
                        border: 2px solid rgba(255,255,255,0.3);
                    }
                """)
            else:
                # 占位：首字母
                self.setText(self._name_en[0] if self._name_en else "?")
                self.setStyleSheet("""
                    QLabel {
                        background-color: #3F51B5;
                        color: white;
                        border-radius: 24px;
                        font-weight: bold;
                        font-size: 18px;
                        border: 2px solid rgba(255,255,255,0.3);
                    }
                """)
                self.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))


class OverlayWindow(QWidget):
    # 点击信号：(champion_name, spell_name)
    spell_clicked = Signal(str, str)

    def __init__(self, model: OverlayModel, parent=None):
        super().__init__(parent)
        self._model = model
        
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
        """自动计算窗口位置，确保完整落在屏幕内。"""
        screen = QApplication.primaryScreen().availableGeometry()
        # 窗口尺寸：宽度 380，高度自适应
        w = 380
        h = 300
        # 右侧留 10px margin，顶部留 80px margin（更靠近右边缘）
        x = screen.width() - w - 10
        y = 80
        # 确保不超出屏幕
        if x + w > screen.width():
            x = screen.width() - w - 5
        if y + h > screen.height():
            y = screen.height() - h - 10
        self.setGeometry(x, y, w, h)
        print(f"[overlay] 窗口位置: x={x}, y={y}, w={w}, h={h}, screen={screen.width()}x{screen.height()}")

    def _clear(self):
        while self._root.count():
            it = self._root.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _skill_dot(self, sk: OverlaySkill) -> QWidget:
        """Q/W/E/R 技能图标（用字母代替真实图标）。"""
        w = QFrame()
        w.setFixedSize(32, 32)
        w.setFrameShape(QFrame.StyledPanel)
        
        letter = sk.skill.upper() if sk.skill else "?"
        
        if sk.active:
            color = "#ff9800" if sk.remaining < 10 else "#ffcc80"
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
            lbl.setFont(QFont("Microsoft YaHei", 7, QFont.Bold))
            layout.addWidget(lbl)
        else:
            w.setStyleSheet("""
                QFrame {
                    background-color: rgba(76, 175, 80, 150);
                    border: 1px solid rgba(255,255,255,0.2);
                    border-radius: 4px;
                }
            """)
            layout = QVBoxLayout(w)
            layout.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(letter)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: white; background: transparent; border: none; font-weight: bold;")
            lbl.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
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
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        
        # 召唤师技能图标
        spell1_icon = ClickableSpellIcon(row.champion_name, row.spell1)
        spell1_icon.clicked.connect(self.spell_clicked.emit)
        layout.addWidget(spell1_icon)
        spell2_icon = ClickableSpellIcon(row.champion_name, row.spell2)
        spell2_icon.clicked.connect(self.spell_clicked.emit)
        layout.addWidget(spell2_icon)
        
        # 最右边：英雄头像（支持死亡状态）
        champ = _champion_catalog.get_by_id(row.champion_id) or _champion_catalog.get_by_name(row.champion_name)
        name_en = champ.name_en if champ else row.champion_name
        avatar = ChampionAvatar(name_en)
        avatar.set_dead(row.is_dead, row.respawn_timer)
        layout.addWidget(avatar)
        
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
