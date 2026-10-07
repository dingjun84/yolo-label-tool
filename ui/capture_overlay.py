"""窗口吸附截图的覆盖层（参考微信截图的交互）。

显示一層覆盖整个虚拟桌面的半透明遮罩，鼠标停到哪个窗口就把哪个窗口
「挖」出来并描边；双击、按 Enter 或点右下角的对勾即可截取该窗口。

只负责**选窗口**，真正的截取与存盘由 MainWindow 完成（``captured`` 信号）。
这样遮罩不必关心落盘位置、命名、列表刷新这些事。

坐标换算
--------
后端给的坐标是它自己坐标系下的值（macOS 逻辑点 / Windows 物理像素），
这里统一换算成 Qt 逻辑坐标：``qt = 主屏原点 + 后端坐标 × scale``，
scale = Qt 主屏宽度 / 后端主屏宽度。
"""

from __future__ import annotations

from PyQt5.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QGuiApplication, QPainter, QPen, QRegion
from PyQt5.QtWidgets import QApplication, QWidget

from core.capture import WindowInfo, create_backend
from i18n.translator import tr

CONFIRM_SIZE = 36
WINDOW_REFRESH_MS = 400
SHADOW_ALPHA = 120
ACCENT = "#2f8ff5"
ACCENT_DARK = "#1b6fc4"


class CaptureOverlay(QWidget):
    """全屏窗口选择遮罩。选中后发 ``captured(WindowInfo)``。"""

    captured = pyqtSignal(object)

    def __init__(self, backend=None, parent=None):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

        self._backend = backend if backend is not None else create_backend()
        self._windows = []
        self._hover = None
        self._confirm_rect = QRect()
        self._button_hover = False
        self._scale = 1.0

        self._setup_geometry()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._reload_windows)
        self._refresh_timer.start(WINDOW_REFRESH_MS)

        self._reload_windows()

    # ------------------------------------------------------------------ 几何
    def _setup_geometry(self):
        union = QRect()
        for screen in QGuiApplication.screens():
            union = union.united(screen.geometry())
        if union.isEmpty():
            union = QRect(0, 0, 1280, 800)
        self.setGeometry(union)

        # 后端坐标系 -> Qt 逻辑坐标
        try:
            primary = QGuiApplication.primaryScreen().geometry()
            backend_width = self._backend.main_screen_width()
            self._scale = (primary.width() / backend_width) if backend_width else 1.0
            self._origin_x = primary.x()
            self._origin_y = primary.y()
        except Exception:
            self._scale = 1.0
            self._origin_x = 0
            self._origin_y = 0

    def _to_qt_rect(self, win: WindowInfo) -> QRect:
        return QRect(
            self._origin_x + int(round(win.x * self._scale)),
            self._origin_y + int(round(win.y * self._scale)),
            max(1, int(round(win.width * self._scale))),
            max(1, int(round(win.height * self._scale))),
        )

    def _to_backend_point(self, pos):
        """窗口坐标 -> 后端坐标系。"""
        if self._scale <= 0:
            return float(pos.x()), float(pos.y())
        return (
            (pos.x() - self._origin_x) / self._scale,
            (pos.y() - self._origin_y) / self._scale,
        )

    # ------------------------------------------------------------------ 窗口
    def _reload_windows(self):
        try:
            self._windows = self._backend.list_windows()
        except Exception:
            self._windows = []

        # 高亮跟着窗口走：按 id 重新绑定，窗口消失就取消高亮
        if self._hover is not None:
            match = next(
                (w for w in self._windows if w.id == self._hover.id), None
            )
            if match != self._hover:
                self._hover = match
                self.update()

    def _hit(self, px, py):
        for win in self._windows:  # 已按 z 序排列，第一个命中的就是最上层
            if win.contains(px, py):
                return win
        return None

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        full = self.rect()
        hole = self._to_qt_rect(self._hover) if self._hover is not None else QRect()

        # 半透明遮罩，把命中窗口的区域挖空
        if not hole.isEmpty():
            painter.setClipRegion(QRegion(full).subtracted(QRegion(hole)))
        painter.fillRect(full, QColor(0, 0, 0, SHADOW_ALPHA))
        painter.setClipping(False)

        if not hole.isEmpty():
            self._paint_hole(painter, hole)

        self._paint_hint(painter)

    def _paint_hole(self, painter: QPainter, hole: QRect):
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(ACCENT), 2))
        painter.drawRect(hole.adjusted(0, 0, -1, -1))
        painter.setPen(QColor(ACCENT))
        painter.setBrush(Qt.NoBrush)

        self._paint_label(painter, hole)
        self._paint_confirm(painter, hole)

    def _paint_label(self, painter: QPainter, hole: QRect):
        win = self._hover
        name = win.display_name()
        if len(name) > 46:
            name = name[:44] + "…"
        text = f"{name}   {win.width}×{win.height}"

        metrics = painter.fontMetrics()
        width = min(metrics.width(text) + 20, max(hole.width() - 12, 160))
        height = metrics.height() + 10

        top = hole.top() + 6
        if hole.height() < height + CONFIRM_SIZE + 16:
            top = max(self.rect().top() + 4, hole.top() - height - 4)

        bar = QRect(hole.left() + 6, top, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(ACCENT))
        painter.drawRoundedRect(bar, 4, 4)

        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            bar.adjusted(10, 0, -8, 0),
            Qt.AlignVCenter | Qt.AlignLeft,
            metrics.elidedText(text, Qt.ElideRight, bar.width() - 18),
        )

    def _paint_confirm(self, painter: QPainter, hole: QRect):
        size = CONFIRM_SIZE
        margin = 10
        cx = hole.right() - size // 2 - margin
        cy = hole.bottom() - size // 2 - margin
        # 空间不够时挪到框外，避免盖住内容
        if hole.width() < size + 2 * margin or hole.height() < size + 2 * margin:
            cx = hole.right() + size // 2 + 2
            cy = hole.bottom() - size // 2

        self._confirm_rect = QRect(
            cx - size // 2, cy - size // 2, size, size
        )

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(ACCENT_DARK if self._button_hover else ACCENT))
        painter.drawEllipse(self._confirm_rect)

        # 手绘对勾，避免字体缺字
        center = self._confirm_rect.center()
        painter.setPen(QPen(QColor("#ffffff"), 2.4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(center.x() - 8, center.y(), center.x() - 2, center.y() + 6)
        painter.drawLine(center.x() - 2, center.y() + 6, center.x() + 8, center.y() - 7)

    def _paint_hint(self, painter: QPainter):
        text = tr("capture.hint")
        metrics = painter.fontMetrics()
        width = metrics.width(text) + 36
        height = metrics.height() + 16

        bar = QRect(
            self.rect().center().x() - width // 2,
            self.rect().top() + 44,
            width,
            height,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(bar, 8, 8)

        painter.setPen(QColor("#ffffff"))
        painter.drawText(bar, Qt.AlignCenter, text)

    # ------------------------------------------------------------------ 事件
    def mouseMoveEvent(self, event):
        backend_x, backend_y = self._to_backend_point(event.pos())
        hover = self._hit(backend_x, backend_y)

        if hover != self._hover:
            self._hover = hover
            self._button_hover = False
            self.update()
            return

        button_hover = (
            not self._confirm_rect.isEmpty()
            and self._confirm_rect.contains(event.pos())
        )
        if button_hover != self._button_hover:
            self._button_hover = button_hover
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._confirm_rect.isEmpty() and self._confirm_rect.contains(event.pos()):
                self._confirm()
                return
            # 左键单击也直接选中鼠标下的窗口，省得移动一下
            backend_x, backend_y = self._to_backend_point(event.pos())
            hover = self._hit(backend_x, backend_y)
            if hover is not None and hover != self._hover:
                self._hover = hover
                self.update()
        elif event.button() == Qt.RightButton:
            self._cancel()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._confirm()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self._cancel()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._confirm()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self.grabKeyboard()  # 保证 Esc / Enter 一定落到这里

    def closeEvent(self, event):
        self.releaseKeyboard()
        self._refresh_timer.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------ 动作
    def _confirm(self):
        win = self._hover
        if win is None:
            return
        # 先把自己藏起来再交给调用方截图，视觉上不会拍到遮罩
        self.hide()
        QApplication.processEvents()
        self.captured.emit(win)
        self.close()

    def _cancel(self):
        self.close()
