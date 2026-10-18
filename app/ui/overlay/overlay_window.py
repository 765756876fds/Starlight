"""OverlayWindow：Qt 透明无边框置顶浮窗（敌方五人 CD + 可点击召唤师技能）。

H.4.4-R2 修正：
- 真实英雄头像（Data Dragon）
- 真实召唤师技能图标（Data Dragon）
- 分层半透明 HUD 风格
- 窗口位置自动适配屏幕
- 召唤师技能可点击，触发 CommandBus → CooldownManager
"""
from __future__ import annotations

import os
import sys
from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens

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

# 调试开关：OVERLAY_FORCE_FULL_REBUILD=1 → 强制每帧全量重建（绕开增量刷新）
# 用途：怀疑"增量漏更新"时开启对比排查
_FORCE_FULL_REBUILD = os.environ.get("OVERLAY_FORCE_FULL_REBUILD") == "1"


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

    def set_spell(self, spell) -> None:
        """增量更新：替换技能状态并重绘（不重建控件，复用现有图标）。"""
        self._spell = spell
        self._update_style()
        self.update()

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
        
        # 加载头像（已预乘 30% 透明度，见 overlay_assets.get_champion_portrait；
        # 不用 QGraphicsOpacityEffect——每帧创建 effect 是主线程重活）
        self._pixmap = get_champion_portrait(name_en)
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
        # 固定尺寸：只读一次，之后不再变化（悬浮窗大小调整功能已移除）
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
        self._root.setContentsMargins(Tokens.SPACING_SM, Tokens.SPACING_SM,
                                      Tokens.SPACING_SM, Tokens.SPACING_SM)
        self._root.setSpacing(Tokens.SPACING_SM)

        # 增量刷新状态：5 张卡创建一次，之后只更新变化的字段
        self._cards: list = []          # 复用的卡片控件（与 rows 一一对应）
        self._last_struct: list = []    # 每卡结构指纹（英雄/技能/缩放，变→全量重建）
        self._last_state: list = []     # 每卡状态指纹（CD/死亡，变→增量更新）

        # 100ms 刷新定时器
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(100)

        self._place_default()
        self._apply_ws_ex_layered()
        self.hide()

    def hideEvent(self, event):
        """隐藏时不停止刷新定时器——_refresh 内部由 _sync_visibility 控制显隐；
        若在此 stop，则一旦 hide 过就再无入口重新 start，overlay 永久消失。"""
        # self._timer.stop()  # 修复：移除，否则 hide 一次后 timer 永不再启动
        super().hideEvent(event)

    def showEvent(self, event):
        """显示时重启刷新定时器。"""
        self._timer.start(100)
        super().showEvent(event)

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

        # 挂增量更新引用（供 _refresh 状态变化时复用，不重建）
        card.avatar = avatar
        card.spell1_icon = spell1_icon
        card.spell2_icon = spell2_icon
        
        return card

    @staticmethod
    def _row_struct(row, scale: float) -> tuple:
        """结构指纹：英雄/技能/缩放变化 → 需要全量重建。"""
        champ = _champion_catalog.get_by_id(row.champion_id) or _champion_catalog.get_by_name(row.champion_name)
        name_en = champ.name_en if champ else row.champion_name
        return (float(scale), row.champion_id, name_en,
                row.spell1.spell_id, row.spell2.spell_id)

    @staticmethod
    def _row_state(row) -> tuple:
        """状态指纹：CD/死亡变化 → 增量更新（不重建控件）。"""
        return (row.is_dead, int(row.respawn_timer),
                row.spell1.active, int(row.spell1.remaining),
                row.spell2.active, int(row.spell2.remaining))

    def _full_rebuild(self, rows) -> None:
        """全量重建所有卡片（结构变化 / 长度变化 / 调试开关强制）。"""
        self._clear()
        self._cards = []
        self._last_struct = []
        self._last_state = []
        for row in rows:
            card = self._build_enemy_card(row)
            self._cards.append(card)
            self._last_struct.append(self._row_struct(row, self._scale))
            self._last_state.append(None)  # 首帧强制走一次增量更新
            self._root.addWidget(card)

    def _sync_visibility(self, rows) -> None:
        if not rows:
            if self.isVisible():
                self.hide()
        else:
            if not self.isVisible():
                self.show()

    def _refresh(self) -> None:
        try:
            rows = self._model.get_rows()
        except Exception:
            import traceback
            traceback.print_exc()
            rows = []

        # 调试开关：强制每帧全量重建（排查"增量是否漏更新"用）
        if _FORCE_FULL_REBUILD:
            self._full_rebuild(rows)
            self._sync_visibility(rows)
            return

        # 长度兜底：行数变化（含空↔非空）→ 全量重建，防漏更新
        if len(self._cards) != len(rows):
            self._full_rebuild(rows)
            self._sync_visibility(rows)
            return

        if not rows:
            self._sync_visibility(rows)
            return

        # 结构指纹：英雄/技能/缩放变化 → 全量重建一次
        structs = [self._row_struct(r, self._scale) for r in rows]
        if structs != self._last_struct:
            self._full_rebuild(rows)
            self._sync_visibility(rows)
            return

        # 状态指纹：CD/死亡变化 → 复用控件，只更新内容
        states = [self._row_state(r) for r in rows]
        for i, row in enumerate(rows):
            if states[i] != self._last_state[i]:
                card = self._cards[i]
                card.avatar.set_dead(row.is_dead, row.respawn_timer)
                card.spell1_icon.set_spell(row.spell1)
                card.spell2_icon.set_spell(row.spell2)
                self._last_state[i] = states[i]

        self._sync_visibility(rows)


