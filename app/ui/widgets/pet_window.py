"""桌面宠物第一阶段框架（仅 UI 呈现，无业务耦合）。

PetWindow：无边框置顶透明窗口，内含 Live2D 渲染承载（QOpenGLWidget）：
- 待机动画：GL 上下文就绪后 live2d.init → 加载模型 → CreateRenderer → Idle 待机循环
- 鼠标按住拖拽窗口
- 贴边隐藏：靠近屏幕左/右/上边缘时隐藏到屏幕外，只留触发条；鼠标回到触发区自动恢复
- 降级：live2d 不可用或模型加载失败时静默隐藏（不留下空白透明窗）

live2d-py 1.0.0 API：live2d.init() / live2d.Model() → LoadModelJson(path) →
CreateRenderer() → StartMotion(group, no, priority) → Update() + Draw()。
"""
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

# 默认测试模型（imuncle/live2d 浅克隆产物）；可参数化传入其他 .model3.json
DEFAULT_MODEL = (r"D:\Live2D_Models\live2d\live2d_3\model"
                 r"\Azue Lane(JP)\lingbo\lingbo.model3.json")
_FRAME_MS = 33          # 约 30fps 待机动画循环
_EDGE_CHECK_MS = 500    # 贴边检测频率
_EDGE_MARGIN = 24       # 距屏幕边缘多远视为"贴边"
_PEEK_WIDTH = 10        # 隐藏后留在屏幕内的触发条宽度
_EDGE_GAP = 8           # 恢复时距边缘的间隙


class PetGLWidget(QOpenGLWidget):
    """Live2D 渲染承载。所有 live2d 调用都在 GL 上下文就绪后执行（裸环境会段错误）。"""

    def __init__(self, model_path: str, parent=None):
        super().__init__(parent)
        self._model_path = model_path
        self._model = None
        self._ready = False

    def initializeGL(self):  # noqa: N802
        try:
            import live2d
            live2d.init()
            self._model = live2d.Model()
            self._model.LoadModelJson(self._model_path)
            self._model.CreateRenderer()
            try:
                self._model.SetAutoBlink(True)
                self._model.SetAutoBreath(True)
            except Exception:
                pass
            # 待机动画：优先 Idle 组第 0 条；无 Idle 组则随机待机
            try:
                self._model.StartMotion("Idle", 0, live2d.MotionPriority.Normal)
            except Exception:
                try:
                    self._model.StartRandomMotion("Idle", live2d.MotionPriority.Normal)
                except Exception:
                    self._model.StartRandomMotion("", live2d.MotionPriority.Normal)
            self._ready = True
            print(f"[Pet] Live2D 模型就绪: {self._model_path}")
        except Exception as e:
            self._ready = False
            print(f"[Pet] Live2D 初始化失败（桌宠降级隐藏）: {e}")

    def paintGL(self):  # noqa: N802
        if not self._ready or self._model is None:
            return
        try:
            import live2d
            live2d.clearBuffer(0, 0, 0, 0)
            self._model.Update()
            self._model.Draw()
        except Exception:
            self._ready = False

    def resizeGL(self, w: int, h: int):  # noqa: N802
        if not self._ready or self._model is None:
            return
        try:
            self._model.Resize(w, h)
            cw, ch = self._model.GetCanvasSizePixel()
            if cw and ch:
                self._model.SetScale(min(w / cw, h / ch))
        except Exception:
            pass


class PetWindow(QWidget):
    """桌宠外壳：无边框置顶透明 + 待机动画循环 + 拖拽 + 贴边隐藏。"""

    def __init__(self, model_path: str = DEFAULT_MODEL, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(240, 360)
        self._gl = PetGLWidget(model_path, self)
        self._gl.setGeometry(0, 0, self.width(), self.height())
        # 待机动画循环：驱动 GL 重绘（Update + Draw）
        self._anim = QTimer(self)
        self._anim.setInterval(_FRAME_MS)
        self._anim.timeout.connect(self._gl.update)
        # 贴边隐藏检测
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(_EDGE_CHECK_MS)
        self._edge_timer.timeout.connect(self._check_edge)
        self._drag_offset: QPoint | None = None
        self._hidden_edge = None       # 隐藏方向："left"/"right"/"top"/None
        self._normal_pos = None        # 隐藏前的窗口位置（恢复用）
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._maybe_hide_if_unready)

    # ---- 生命周期 ----
    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._anim.start()
        self._edge_timer.start()
        # GL 若在 2s 内未就绪（live2d 缺失/模型损坏）→ 降级隐藏，避免空白透明窗
        self._show_timer.start(2000)

    def closeEvent(self, event):  # noqa: N802
        self._anim.stop()
        self._edge_timer.stop()
        super().closeEvent(event)

    def _maybe_hide_if_unready(self) -> None:
        if not self._gl._ready:
            print("[Pet] 模型未就绪，桌宠窗口降级隐藏")
            self.hide()

    # ---- 鼠标拖拽 ----
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            # 拖拽即视为"用户意图恢复"，清除贴边隐藏态
            self._hidden_edge = None
            self._normal_pos = None

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_offset = None

    # ---- 贴边隐藏/恢复 ----
    def _target_hidden_pos(self, geo) -> tuple:
        """贴边隐藏目标位置（留触发条在屏幕内）。geo=屏幕 availableGeometry。"""
        w, h = self.width(), self.height()
        if self._hidden_edge == "left":
            return (geo.left() - w + _PEEK_WIDTH, self.y())
        if self._hidden_edge == "right":
            return (geo.right() - _PEEK_WIDTH, self.y())
        if self._hidden_edge == "top":
            return (self.x(), geo.top() - h + _PEEK_WIDTH)
        return (self.x(), self.y())

    def _check_edge(self) -> None:
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        pos = self.frameGeometry()
        mx = self.cursor().pos()
        mouse_on_window = (pos.adjusted(-_PEEK_WIDTH, -_PEEK_WIDTH,
                                       _PEEK_WIDTH, _PEEK_WIDTH).contains(mx))
        # 贴边判定：窗口左上角贴近某侧屏幕边缘
        near_left = pos.left() <= geo.left() + _EDGE_MARGIN
        near_right = pos.right() >= geo.right() - _EDGE_MARGIN
        near_top = pos.top() <= geo.top() + _EDGE_MARGIN
        if mouse_on_window:
            # 鼠标在窗口/触发区上：若处于隐藏态则恢复
            if self._hidden_edge and self._normal_pos is not None:
                self.move(self._normal_pos[0], self._normal_pos[1])
                self._hidden_edge = None
            return
        if self._hidden_edge:
            # 已隐藏：检查鼠标是否进入触发条区域 → 恢复
            if self._mouse_in_peek_strip(geo):
                if self._normal_pos is not None:
                    self.move(self._normal_pos[0], self._normal_pos[1])
                    self._hidden_edge = None
            return
        # 未隐藏：贴近边缘且鼠标不在窗口上 → 隐藏
        edge = None
        if near_left:
            edge = "left"
        elif near_right:
            edge = "right"
        elif near_top:
            edge = "top"
        if edge:
            self._normal_pos = (self.x(), self.y())
            self._hidden_edge = edge
            self.move(*self._target_hidden_pos(geo))

    def _mouse_in_peek_strip(self, geo) -> bool:
        """鼠标是否位于贴边触发条（屏幕边缘 _PEEK_WIDTH 宽的条带）。"""
        mx = self.cursor().pos()
        if self._hidden_edge == "left":
            return geo.left() <= mx.x() <= geo.left() + _PEEK_WIDTH and \
                self.y() <= mx.y() <= self.y() + self.height()
        if self._hidden_edge == "right":
            return geo.right() - _PEEK_WIDTH <= mx.x() <= geo.right() and \
                self.y() <= mx.y() <= self.y() + self.height()
        if self._hidden_edge == "top":
            return self.x() <= mx.x() <= self.x() + self.width() and \
                geo.top() <= mx.y() <= geo.top() + _PEEK_WIDTH
        return False

    # ---- 外部控制 ----
    def set_visible_with_pet(self, on: bool) -> None:
        """开关桌宠：显示时保持贴边状态由 _check_edge 维护；关闭则彻底隐藏。"""
        if on:
            self.show()
        else:
            self.hide()
            self._anim.stop()
            self._edge_timer.stop()
