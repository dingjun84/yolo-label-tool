"""窗口吸附截图的覆盖层（参考微信截图的交互）。

鼠标停到哪个窗口就把哪个窗口「挖」出来并描边；双击、按 Enter 或点右下角的
对勾即可截取该窗口，Esc 取消。

只负责**选窗口**，真正的截取与存盘由 MainWindow 完成（``captured`` 信号）。
这样遮罩不必关心落盘位置、命名、列表刷新这些事。

为什么是「一屏一个面板」
------------------------
把遮罩做成一个跨屏大窗口在 macOS 上不可靠：``setGeometry(所有屏幕的并集)`` 会被
平台挪走 —— 多屏 + 混合 DPI 实测下，请求 (0,-381,4096,1440) 被挪成 (1536,-282,4096,1440)
（主屏整块漏掉），而且每次纠正后还会被再挪回去。贴在单块屏幕内的窗口没有这个问题，
所以这里给每块屏各开一个无边框面板，状态由 ``CaptureOverlay`` 统一持有，
``_CapturePane`` 只负责绘制与转发事件。

坐标
----
后端给的坐标是它自己坐标系下的值（macOS 逻辑点 / Windows 物理像素）::

    Qt 全局 = 主屏原点 + 后端坐标 × scale

scale = Qt 主屏宽度 / 后端主屏宽度。绘制一律在 **Qt 全局坐标** 里算，各面板把画笔
平移到全局坐标、靠窗口自身裁剪露出属于自己的那部分，于是跨屏的描边/对勾会被两块
面板无缝拼起来。鼠标位置一律取 ``event.globalPos()``，与面板实际摆放无关。
"""

from __future__ import annotations

import sys
import time

from PyQt5.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QRegion
from PyQt5.QtWidgets import QApplication, QWidget

from core.capture import create_backend
from i18n.translator import tr

CONFIRM_SIZE = 36
WINDOW_REFRESH_MS = 400
CURSOR_POLL_MS = 30  # 光标轮询间隔：mouseMoveEvent 被抢时的高亮兜底
SHADOW_ALPHA = 120
ACCENT = "#2f8ff5"
ACCENT_DARK = "#1b6fc4"
# 面板显示后再核对一次自己的位置（实测跨屏窗口会被平台挪走，单屏窗口一般不会）
PANE_GEOMETRY_RETRY_MS = 150


class _CapturePane(QWidget):
    """一块屏幕上的遮罩面板：只画自己这块，事件交给 ``CaptureOverlay``。"""

    def __init__(self, owner: "CaptureOverlay", screen_rect: QRect):
        super().__init__(None)
        self._owner = owner
        self._screen_rect = QRect(screen_rect)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen_rect)

        self._geometry_checked = False

    # ---------------------------------------------------------------- 坐标
    def _local(self, global_rect: QRect) -> QRect:
        """全局矩形 -> 本面板局部矩形。"""
        return global_rect.translated(-self.pos().x(), -self.pos().y())

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, _event):
        owner = self._owner
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        hole = owner.hover_rect()
        full = self.rect()
        local_hole = self._local(hole) if not hole.isEmpty() else QRect()

        # 半透明遮罩，把命中窗口的区域挖空（跨屏时两块面板各挖各自那半）
        if not local_hole.isEmpty():
            painter.setClipRegion(QRegion(full).subtracted(QRegion(local_hole)))
        painter.fillRect(full, QColor(0, 0, 0, SHADOW_ALPHA))
        painter.setClipping(False)
        # 挖洞区必须留一层 alpha>0 的像素：layered 窗口 alpha=0 的像素
        # 会被系统做鼠标穿透，双击确认会打到洞下面的真实窗口上
        # （既截不了图，又误操作别的应用）。alpha=1 视觉上不可见。
        if not local_hole.isEmpty():
            painter.fillRect(local_hole, QColor(0, 0, 0, 1))

        # 描边/标签/对勾都在全局坐标里画，超出本面板的部分由窗口自己裁掉
        painter.translate(-self.pos().x(), -self.pos().y())
        if not hole.isEmpty():
            owner.paint_window_decorations(painter, hole)
        owner.paint_hint(painter)

    # ---------------------------------------------------------------- 事件
    def mouseMoveEvent(self, event):
        self._owner.handle_move(event.globalPos())

    def mousePressEvent(self, event):
        self._owner.handle_press(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._owner.confirm()

    def keyPressEvent(self, event):
        if not self._owner.handle_key(event.key()):
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        # 平台偶尔会在显示后重摆窗口，核对一次就够（绘制/命中不依赖它）
        if not self._geometry_checked:
            self._geometry_checked = True
            QTimer.singleShot(PANE_GEOMETRY_RETRY_MS, self._restore_geometry)

    def closeEvent(self, event):
        self.releaseKeyboard()
        super().closeEvent(event)

    def _restore_geometry(self):
        if self.isVisible() and self.geometry() != self._screen_rect:
            self.setGeometry(self._screen_rect)


class CaptureOverlay(QObject):
    """全屏窗口选择遮罩（每块屏幕一个面板）。选中后发 ``captured(WindowInfo)``。"""

    captured = pyqtSignal(object)

    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        self._backend = backend if backend is not None else create_backend()
        self._windows = []
        self._hover = None
        self._union = QRect()          # 所有屏幕的并集（全局坐标）
        self._confirm_rect = QRect()   # 对勾按钮（全局坐标）
        self._button_hover = False
        self._scale = 1.0
        self._origin_x = 0
        self._origin_y = 0
        self._own_ids = frozenset()    # 本遮罩各面板的原生窗口 id
        self._confirmed = False        # 防止双击/Enter/对勾触发两次截图
        self._last_press_at = None     # 自判双击用
        self._last_press_pos = QPoint()
        self._last_cursor = QPoint(-1, -1)  # 轮询到的上次光标位

        self._setup_geometry()

        self._panes = [
            _CapturePane(self, screen.geometry())
            for screen in QGuiApplication.screens()
        ]
        if not self._panes:
            self._panes = [_CapturePane(self, QRect(0, 0, 1280, 800))]

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._reload_windows)
        self._refresh_timer.start(WINDOW_REFRESH_MS)

        # 光标轮询兜底：系统把 mouseMove 派给更高 z 序窗口时仍能驱动高亮
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cursor)
        self._poll_timer.start(CURSOR_POLL_MS)

        self._reload_windows()

    # ------------------------------------------------------------------ 几何
    def _setup_geometry(self):
        union = QRect()
        for screen in QGuiApplication.screens():
            union = union.united(screen.geometry())
        if union.isEmpty():
            union = QRect(0, 0, 1280, 800)
        self._union = union

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

    def to_backend(self, global_pos: QPoint):
        """Qt 全局坐标 -> 后端坐标系。"""
        if self._scale <= 0:
            return float(global_pos.x()), float(global_pos.y())
        return (
            (global_pos.x() - self._origin_x) / self._scale,
            (global_pos.y() - self._origin_y) / self._scale,
        )

    def hover_rect(self) -> QRect:
        """悬停窗口的全局矩形；没有悬停时返回空矩形。"""
        if self._hover is None:
            return QRect()
        return QRect(
            self._origin_x + int(round(self._hover.x * self._scale)),
            self._origin_y + int(round(self._hover.y * self._scale)),
            max(1, int(round(self._hover.width * self._scale))),
            max(1, int(round(self._hover.height * self._scale))),
        )

    # ---------------------------------------------------------------- 自身身份
    def _own_exclude(self):
        """枚举窗口时要排除的本进程 id（就是遮罩自己的这些面板）。

        取不到（或只取到一部分）时返回 ``None``，让后端退回保守策略 —— 本进程窗口
        一律不列出。最坏情况只是回到旧行为，绝不会把遮罩自己列成候选窗口。
        """
        if self._own_ids:
            return self._own_ids
        ids = set()
        for pane in self._panes:
            wid = self._pane_native_id(pane)
            if wid is None:
                return None
            ids.add(wid)
        self._own_ids = frozenset(ids)
        return self._own_ids

    @staticmethod
    def _pane_native_id(pane: _CapturePane):
        """面板窗口的原生编号：macOS 是 CGWindowID，其它平台就是 HWND。"""
        try:
            handle = int(pane.winId())  # macOS 上这是 NSView*，Windows 上是 HWND
        except Exception:
            return None
        if sys.platform != "darwin":
            return handle
        try:
            import objc
            from AppKit import NSApplication

            # macOS 的 winId() 不是 CGWindowID，得拿 NSView 去 NSApp 里反查。
            for window in NSApplication.sharedApplication().windows() or []:
                view = window.contentView()
                if view is not None and int(objc.pyobjc_id(view)) == handle:
                    return int(window.windowNumber())
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ 窗口
    def _reload_windows(self):
        try:
            self._windows = self._backend.list_windows(
                own_exclude=self._own_exclude()
            )
        except Exception:
            self._windows = []

        # 高亮跟着窗口走：按 id 重新绑定，窗口消失就取消高亮
        if self._hover is not None:
            match = next(
                (w for w in self._windows if w.id == self._hover.id), None
            )
            if match != self._hover:
                self._hover = match
                self._confirm_rect = QRect()
                self._repaint()

    def _hit(self, px, py):
        for win in self._windows:  # 已按 z 序排列，第一个命中的就是最上层
            if win.contains(px, py):
                return win
        return None

    # ------------------------------------------------------------------ 轮询兜底
    def _poll_cursor(self):
        """光标轮询：mouseMoveEvent 被更高 z 序窗口抢走时仍能驱动高亮。

        实测（Windows + Electron/Chromium 置顶应用）：遮罩显示后，其他
        topmost 窗口会间歇性把遮罩面板压到下面，系统把 WM_MOUSEMOVE 派给
        它们 —— 表现为「移动鼠标，高亮窗口不变」。这里每 CURSOR_POLL_MS
        主动读一次 QCursor.pos()：

        1. 光标位置变了就跑一遍 handle_move（幂等，hover 不变不重绘），
           与 mouseMoveEvent 双驱动互补；
        2. 检查光标处的本进程 widget 是否还是遮罩面板；不是（被主窗口或
           其他进程的 topmost 窗口盖住）就立即夺回顶层 —— 保证后续
           点击/双击落在遮罩上，不会误点到别的应用。
        """
        pos = QCursor.pos()
        if pos != self._last_cursor:
            self._last_cursor = pos
            self.handle_move(pos)

        covered = QApplication.widgetAt(pos)
        need_raise = (
            (covered is None and self._union.contains(pos))
            or (covered is not None
                and not any(covered is p for p in self._panes))
        )
        if need_raise:
            self._ensure_on_top()

    def _ensure_on_top(self):
        """把所有面板重新插到系统 topmost 组的最前。

        注意不能用 Qt 的 raise_()：它只做 SetWindowPos(HWND_TOP)，对已带
        WS_EX_TOPMOST 的窗口**不会改变其在 topmost 组内的位置**，压不过
        其他 topmost 窗口（Electron 置顶应用等）。必须用 HWND_TOPMOST
        重设一次，Windows 才会把它重新插到 topmost 组顶。
        """
        if sys.platform.startswith("win"):
            try:
                import ctypes
                from ctypes import wintypes as wt

                user32 = ctypes.WinDLL("user32")
                # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                flags = 0x0001 | 0x0002 | 0x0010
                for pane in self._panes:
                    hwnd = wt.HWND(int(pane.winId()))
                    user32.SetWindowPos(
                        hwnd, wt.HWND(-1),  # HWND_TOPMOST
                        0, 0, 0, 0, flags,
                    )
            except Exception:
                for pane in self._panes:  # 退化：总比不 raise 好
                    pane.raise_()
        else:
            for pane in self._panes:
                pane.raise_()

    def _repaint(self):
        for pane in self._panes:
            pane.update()

    # ------------------------------------------------------------------ 绘制
    def paint_window_decorations(self, painter: QPainter, hole: QRect):
        """在全局坐标里画描边、名称标签和对勾按钮。"""
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
            top = max(self._union.top() + 4, hole.top() - height - 4)

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

    def paint_hint(self, painter: QPainter):
        text = tr("capture.hint")
        metrics = painter.fontMetrics()
        width = metrics.width(text) + 36
        height = metrics.height() + 16

        bar = QRect(
            self._union.center().x() - width // 2,
            self._union.top() + 44,
            width,
            height,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(bar, 8, 8)

        painter.setPen(QColor("#ffffff"))
        painter.drawText(bar, Qt.AlignCenter, text)

    # ------------------------------------------------------------------ 事件
    def handle_move(self, global_pos: QPoint):
        hover = self._hit(*self.to_backend(global_pos))

        if hover != self._hover:
            self._hover = hover
            self._button_hover = False
            self._confirm_rect = QRect()  # 下一次绘制会按新位置重算
            self._repaint()
            return

        button_hover = (
            not self._confirm_rect.isEmpty()
            and self._confirm_rect.contains(global_pos)
        )
        if button_hover != self._button_hover:
            self._button_hover = button_hover
            self._repaint()

    def handle_press(self, event):
        if self._confirmed:
            return
        if event.button() == Qt.RightButton:
            self.cancel()
            return
        if event.button() != Qt.LeftButton:
            return

        pos = event.globalPos()

        # 1) 点中右下角的对勾
        if not self._confirm_rect.isEmpty() and self._confirm_rect.contains(pos):
            self.confirm()
            return

        # 2) 自己判一次双击。有些情况下平台不会补发 MouseButtonDblClick
        #    （典型是第一下点击被系统用来激活窗口），只靠 mouseDoubleClickEvent 会漏。
        now = time.monotonic()
        if (
            self._last_press_at is not None
            and (now - self._last_press_at) * 1000 <= QApplication.doubleClickInterval()
            and (pos - self._last_press_pos).manhattanLength()
            <= QApplication.startDragDistance()
        ):
            self._last_press_at = None
            self.confirm()
            return
        self._last_press_at = now
        self._last_press_pos = pos

        # 3) 单击只更新命中，省得移动一下
        hover = self._hit(*self.to_backend(pos))
        if hover is not None and hover != self._hover:
            self._hover = hover
            self._confirm_rect = QRect()
            self._repaint()

    def handle_key(self, key) -> bool:
        if key == Qt.Key_Escape:
            self.cancel()
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.confirm()
            return True
        return False

    # ------------------------------------------------------------------ 生命周期
    def show(self):
        for pane in self._panes:
            pane.show()
        active = self._active_pane()
        for pane in self._panes:
            pane.raise_()
        active.activateWindow()
        active.setFocus()
        active.grabKeyboard()  # 保证 Esc / Enter 一定落到某块面板

    def hide(self):
        for pane in self._panes:
            pane.hide()

    def close(self):
        self._poll_timer.stop()
        self._refresh_timer.stop()
        for pane in self._panes:
            pane.releaseKeyboard()
            pane.close()
        self._panes = []

    def _active_pane(self) -> _CapturePane:
        """鼠标所在那块屏的面板；找不到就退回第一块。"""
        cursor = QCursor.pos()
        for pane in self._panes:
            if pane.geometry().contains(cursor):
                return pane
        return self._panes[0]

    # ------------------------------------------------------------------ 动作
    def confirm(self):
        if self._confirmed:
            return
        win = self._hover
        if win is None:
            self._last_press_at = None  # 没选中就不算双击，等下一次
            return
        self._confirmed = True
        # 先把自己藏起来再交给调用方截图，视觉上不会拍到遮罩
        self.hide()
        QApplication.processEvents()
        self.captured.emit(win)
        self.close()

    def cancel(self):
        self._confirmed = True
        self.close()
