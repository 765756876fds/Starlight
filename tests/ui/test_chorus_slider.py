from PySide6.QtCore import Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolTip
import pytest

from app.music.online.chorus import KuwoChorus
from app.ui.widgets.waveform_slider import WaveformSlider


@pytest.mark.ui
def test_marker_click_is_separate_from_groove_and_stale_press():
    app = QApplication.instance() or QApplication([])
    slider = WaveformSlider()
    slider.resize(600, 40)
    slider.show()
    try:
        marker = KuwoChorus(1, "5886682", 69.358, 94.837, 324)
        slider.set_chorus((marker, 324))
        pt = slider._chorus_rect().center().toPoint()
        clicked, released = [], []
        slider.chorusClicked.connect(clicked.append)
        slider.sliderReleased.connect(lambda: released.append(slider.value()))
        app.sendEvent(slider, QHelpEvent(QEvent.ToolTip, pt, slider.mapToGlobal(pt)))
        assert "1:09–1:34" in QToolTip.text()
        QToolTip.hideText()
        QTest.mouseClick(slider, Qt.LeftButton, pos=pt)
        assert clicked == [marker] and released == []
        QTest.mousePress(slider, Qt.LeftButton, pos=pt)
        slider.set_chorus(None)
        QTest.mouseRelease(slider, Qt.LeftButton, pos=pt)
        assert clicked == [marker] and released == []
        QTest.mouseClick(slider, Qt.LeftButton, pos=slider.rect().center())
        assert len(released) == 1 and 495 <= released[0] <= 505
        old = slider.value()
        QTest.keyClick(slider, Qt.Key_Right)
        assert slider.value() > old
    finally:
        slider.close()
