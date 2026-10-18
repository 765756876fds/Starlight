"""动效规范（单一来源，翻译自 diseno-qt / ui-ux-pro-max motion 数据库）。

曲线集中在一个模块（diseno-qt §3）：项目内所有动画引用这里的常量，
禁止在各面板/Delegate 中重复写 QEasingCurve 字面量。

曲线参数（Emil Kowalski → Qt BezierSpline，diseno-qt §3）：
- EASE_OUT     (0.23, 1, 0.32, 1)   进入/离开（几乎一切）
- EASE_IN_OUT  (0.77, 0, 0.175, 1)  已在屏幕上的移动
- EASE_CAJON   (0.32, 0.72, 0, 1)   侧边/底部抽屉
- 界面动画 < 300ms；绝不用 In*（InQuad…）——开始慢恰好撞上用户注视时刻。

时长（diseno-qt §4 / qt-ui-design §1.1 / ui-ux-pro-max motion.csv）：
- hover 微交互 150ms（小元素 100–150ms；hover 200ms 上限）
- 面板/卡片 200–300ms
- 骨架屏 shimmer 循环 1200–1600ms（motion.csv #15：gradient sweep, sine.inOut）

Reduced-motion（diseno-qt §6）：Qt 无 prefers-reduced-motion，双通道——
Windows 系统设置（SPI_GETCLIENTAREAANIMATION）+ app 级覆盖开关。
"""
import sys

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QObject, QPointF, QVariantAnimation, Signal,
)

# ---------------------------------------------------------------- 曲线


def _bezier(x1: float, y1: float, x2: float, y2: float) -> QEasingCurve:
    """三次贝塞尔样条（Qt 内置 OutCubic 等过弱，见 diseno-qt §3）。"""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1.0, 1.0))
    return curve


EASE_OUT = _bezier(0.23, 1, 0.32, 1)        # 进入/离开 — 默认
EASE_IN_OUT = _bezier(0.77, 0, 0.175, 1)    # 已在屏上移动
EASE_CAJON = _bezier(0.32, 0.72, 0, 1)      # 侧边/底部抽屉

# ---------------------------------------------------------------- 时长

DUR_HOVER = 150        # hover 微交互（小元素 100–150ms）
DUR_CARD = 250         # 面板/卡片 200–300ms
DUR_TOAST = 250        # 浮窗进出
DUR_EXIT = 200         # 离开比进入快 ~20%（diseno-qt §4）
SHIMMER_DURATION = 1400  # 骨架 shimmer 循环（motion.csv #15: 1200–1600ms）

# ---------------------------------------------------------------- reduced motion

_forced_reduced = None  # True/False/None（None = 跟随系统）


def system_prefers_reduced_motion() -> bool:
    """Windows「显示动画」设置（diseno-qt §6 原样）。非 Windows 返回 False。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        SPI_GETCLIENTAREAANIMATION = 0x1042
        active = ctypes.c_bool(True)
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(active), 0)
        return not active.value
    except Exception:
        return False


def reduced_motion_enabled() -> bool:
    """app 覆盖开关优先；否则跟随系统（diseno-qt §6）。"""
    if _forced_reduced is not None:
        return _forced_reduced
    return system_prefers_reduced_motion()


def set_reduced_motion(force):
    """app 设置覆盖：True=减少动画 / False=允许 / None=跟随系统。"""
    global _forced_reduced
    _forced_reduced = force


# ---------------------------------------------------------------- 骨架时钟

class ShimmerClock(QObject):
    """全局骨架微光时钟（ui-ux-pro-max motion.csv #15 的 Qt 实现）。

    仅在存在「封面加载中」的卡片时运行（acquire/release 引用计数），
    内容加载完或界面不可见即停，避免持续空转；reduced-motion 时静态。
    tick 值 0..1，InOutSine 循环，1400ms 一周。
    """

    tick = Signal(float)
    _inst = None

    @classmethod
    def instance(cls) -> "ShimmerClock":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        super().__init__()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(SHIMMER_DURATION)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.valueChanged.connect(self._on_tick)
        self._refs = 0

    def _on_tick(self, value):
        self.tick.emit(float(value))

    def acquire(self) -> None:
        self._refs += 1
        if self._refs == 1 and not reduced_motion_enabled():
            self._anim.start()

    def release(self) -> None:
        if self._refs > 0:
            self._refs -= 1
            if self._refs == 0:
                self._anim.stop()

    def active(self) -> bool:
        return self._anim.state() == QAbstractAnimation.State.Running

    def pos(self) -> float:
        """0..1 扫描相位；未运行时 0（静态骨架）。"""
        if not self.active():
            return 0.0
        return float(self._anim.currentValue())
