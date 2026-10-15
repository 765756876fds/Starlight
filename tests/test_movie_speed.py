"""包2c：倍速档位细化——B站/爱奇艺标准 9 档 + 弹菜单选档 + 当前档高亮（offscreen）。

覆盖：
- 9 档齐全且顺序正确（0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 1.75 / 2.0 / 2.5 / 3.0）
- _apply_speed：应用档位 → set_speed + 按钮文字（分数/整数两种 label 格式）
- _on_speed：弹菜单（9 个 action、当前档勾选高亮、触发回调 → _apply_speed）
- 倍速失败 → 状态栏降级不崩
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import MagicMock, patch

from app.ui.panels.movie_panel import MoviePanel


class TestMovieSpeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        movie = MagicMock()
        with patch("app.ui.panels.movie_panel.MovieController", return_value=movie):
            panel = MoviePanel(movie=None)
        return panel, movie

    def test_speed_steps_full_9_gears(self):
        panel, _ = self._panel()
        self.assertEqual(
            panel._speed_steps,
            [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0],
        )
        self.assertEqual(panel._speed_idx, 2)  # 默认 1.0x

    def test_apply_speed_fraction_label(self):
        panel, movie = self._panel()
        panel._apply_speed(1.5, 4)
        self.assertEqual(panel._speed_idx, 4)
        self.assertEqual(panel.btn_speed.text(), "倍速 1.5x")
        movie.set_speed.assert_called_once_with(1.5)

    def test_apply_speed_int_label(self):
        panel, movie = self._panel()
        panel._apply_speed(2.0, 6)
        self.assertEqual(panel.btn_speed.text(), "倍速 2x")
        movie.set_speed.assert_called_once_with(2.0)

    def test_apply_speed_by_rate_without_idx(self):
        panel, movie = self._panel()
        panel._apply_speed(2.5)
        self.assertEqual(panel._speed_idx, 7)
        movie.set_speed.assert_called_once_with(2.5)

    def test_apply_speed_failure_falls_back_status(self):
        panel, movie = self._panel()
        movie.set_speed.side_effect = RuntimeError("no mpv")
        panel._apply_speed(3.0, 8)
        self.assertIn("倍速失败", panel.lbl_status.text())

    def test_speed_label_formats(self):
        panel, _ = self._panel()
        labels = [panel._speed_label(r) for r in panel._speed_steps]
        self.assertEqual(labels, ["0.5x", "0.75x", "1x", "1.25x", "1.5x",
                                  "1.75x", "2x", "2.5x", "3x"])

    def test_menu_9_actions_current_highlighted(self):
        """_on_speed 弹菜单：9 个 action、当前档勾选高亮、点档位回调 → _apply_speed。"""
        panel, movie = self._panel()
        panel._speed_idx = 4  # 当前档 1.5x
        fake_menu = MagicMock()
        actions = []

        def add_action(label):
            act = MagicMock()
            act.text = label
            actions.append(act)
            return act

        fake_menu.addAction.side_effect = add_action
        fake_menu.exec_.return_value = None
        with patch("app.ui.panels.movie_panel.QMenu", return_value=fake_menu), \
             patch.object(panel.btn_speed, "mapToGlobal", return_value=MagicMock()):
            panel._on_speed()
        fake_menu.exec_.assert_called_once()
        self.assertEqual(len(actions), 9)
        self.assertEqual([a.text for a in actions],
                         ["0.5x", "0.75x", "1x", "1.25x", "1.5x",
                          "1.75x", "2x", "2.5x", "3x"])
        for act in actions:
            act.setCheckable.assert_called_once_with(True)
        # 当前档（idx4=1.5x）勾选 True，其余 False
        self.assertTrue(actions[4].setChecked.call_args[0][0])
        self.assertFalse(actions[3].setChecked.call_args[0][0])
        self.assertFalse(actions[8].setChecked.call_args[0][0])
        # 点"3x"（idx8）→ 回调触发 _apply_speed(3.0, 8)
        cb = actions[8].triggered.connect.call_args[0][0]
        with patch.object(panel, "_apply_speed") as ap:
            cb(False)
        ap.assert_called_once_with(3.0, 8)


if __name__ == "__main__":
    unittest.main()
