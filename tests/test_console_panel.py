"""ConsolePanel 定向测试：日志降噪分级 + 节流队列 + 行数上限。"""
import pytest

from app.ui.panels.console_panel import ConsolePanel, classify_log, MAX_LINES


def _qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    return app


# ---- 过滤与分级 ----

class TestClassify:
    def test_noise_dropped(self):
        keep, level = classify_log("QPropertyAnimation: you must set a start and end value")
        assert keep is False and level is None

    def test_animation_noise_even_with_prefix(self):
        keep, _ = classify_log("[Music] hover_t animation paint() 0.5")
        assert keep is False

    def test_boot_progress_kept(self):
        keep, level = classify_log("[0/5] 初始化中…")
        assert keep is True
        assert level in ("white", "green")

    def test_asr_ready_green(self):
        keep, level = classify_log("[ASR] 已就绪")
        assert keep is True and level == "green"

    def test_voice_intent_kept(self):
        keep, _ = classify_log("[Voice] 识别: 播放周杰伦")
        assert keep is True
        keep2, _ = classify_log("[Intent] 意图: music.play")
        assert keep2 is True

    def test_error_red(self):
        keep, level = classify_log("[Error] 重连失败: timeout")
        assert keep is True and level == "red"

    def test_warning_yellow(self):
        keep, level = classify_log("[WARN] mpv 未连接，降级")
        assert keep is True and level == "yellow"

    def test_ongoing_event_kept(self):
        keep, level = classify_log("[Ongoing] 加载完成")
        assert keep is True and level == "green"

    def test_gamealarm_kept(self):
        keep, _ = classify_log("[GameAlarm] 开始")
        assert keep is True

    def test_generic_poll_noise_dropped(self):
        keep, _ = classify_log("[LCU] poll status: 0")
        assert keep is False  # 底层轮询噪音

    # ---- 新增黑名单：Combo 心跳 / UI 切换 / mutagen ----
    def test_combo_heartbeat_dropped(self):
        keep, _ = classify_log("[Combo] 键盘监听疑似掉线，正在自动重连…")
        assert keep is False

    def test_combo_autoreconnect_dropped(self):
        keep, _ = classify_log("[Combo] 自动重连成功")
        assert keep is False

    def test_combo_startup_dropped(self):
        keep, _ = classify_log("[Combo] ComboManager 已启动（默认关闭）")
        assert keep is False

    def test_ui_switch_dropped(self):
        keep, _ = classify_log("[UI] switch_to_tabs")
        assert keep is False
        keep2, _ = classify_log("[UI] switched")
        assert keep2 is False

    def test_mutagen_fail_dropped(self):
        keep, _ = classify_log("[Scanner] mutagen 读取失败: 无法解析标签")
        assert keep is False

    def test_scanner_verify_fail_kept(self):
        # 可播验证失败保留（用户点名）
        keep, _ = classify_log("[Scanner] 可播验证失败: 文件损坏")
        assert keep is True

    # ---- 关键操作必须保留 ----
    def test_voice_kept(self):
        keep, _ = classify_log("[Voice] 识别: 播放周杰伦")
        assert keep is True

    def test_interrupt_kept(self):
        keep, _ = classify_log("[Interrupt-Perf] Queue: 0.12s, ASR: 0.88s")
        assert keep is True

    def test_error_warn_kept(self):
        keep, _ = classify_log("[ERROR] 解析失败")
        assert keep is True
        keep2, _ = classify_log("[WARN] 未连接")
        assert keep2 is True


# ---- 节流队列与上限 ----

class TestThrottleQueue:
    def test_queue_maxlen_200(self):
        with ConsolePanel._qlock:
            ConsolePanel._queue.clear()
            for i in range(300):
                ConsolePanel._queue.append((f"[Music] 在线找到 {i}", "green"))
        assert len(ConsolePanel._queue) == MAX_LINES

    def test_drain_renders_and_clears(self):
        _qapp()
        panel = ConsolePanel()
        with ConsolePanel._qlock:
            ConsolePanel._queue.clear()
            ConsolePanel._queue.append(("[ASR] 已就绪", "green"))
            ConsolePanel._queue.append(("[Error] 解析失败", "red"))
        panel._drain()
        assert len(ConsolePanel._queue) == 0
        assert "已就绪" in panel.view.toPlainText()
        assert "解析失败" in panel.view.toPlainText()
        panel.deleteLater()

    def test_view_line_limit(self):
        _qapp()
        panel = ConsolePanel()
        with ConsolePanel._qlock:
            ConsolePanel._queue.clear()
            for i in range(MAX_LINES + 30):
                ConsolePanel._queue.append((f"[Music] 在线找到 {i}", "green"))
        panel._drain()
        # QPlainTextEdit maximumBlockCount 兜底：不会超过 200 行
        assert panel.view.blockCount() <= MAX_LINES
        panel.deleteLater()

    def test_clear_log(self):
        _qapp()
        panel = ConsolePanel()
        with ConsolePanel._qlock:
            ConsolePanel._queue.clear()
            ConsolePanel._queue.append(("[ASR] 已就绪", "green"))
        panel._drain()
        panel.clear_log()
        assert panel.view.toPlainText() == ""
        assert len(ConsolePanel._queue) == 0
        panel.deleteLater()
