"""Windows 窗口捕获后端（user32 / gdi32，纯 ctypes）。

不引入 pywin32 等第三方依赖：枚举用 ``EnumWindows``，抓图用 ``PrintWindow``
（``PW_RENDERFULLCONTENT``，能拿到 DirectComposition 合成的内容），
失败或整幅全黑时退回 ``BitBlt``。

坐标是**物理像素**（虚拟桌面坐标，原点在主显示器左上）。若系统开了显示缩放，
覆盖层会用主屏宽度之比把它换算成 Qt 逻辑坐标。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from . import (
    MIN_WINDOW_SIZE,
    CaptureError,
    WindowBackend,
    WindowInfo,
)

if not sys.platform.startswith("win"):  # pragma: no cover - 只在误导入时触发
    raise ImportError("core.capture.windows 只能在 Windows 上导入")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
SM_CXSCREEN = 0

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class WindowsWindowBackend(WindowBackend):
    name = "windows-gdi"

    # ------------------------------------------------------------------ 可用性
    def available(self):
        if not sys.platform.startswith("win"):
            return False, "当前不是 Windows 平台"
        return True, ""

    # ------------------------------------------------------------------ 枚举
    def list_windows(self):
        results = []
        my_pid = int(kernel32.GetCurrentProcessId())

        def _collect(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return True

                ex_style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
                if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
                    return True

                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if int(pid.value) == my_pid:
                    return True  # 跳过自己（含截图覆盖层）

                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width < MIN_WINDOW_SIZE or height < MIN_WINDOW_SIZE:
                    return True

                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                class_name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_name, 256)

                results.append(WindowInfo(
                    id=int(hwnd),
                    title=title.value,
                    app=class_name.value,
                    x=int(rect.left),
                    y=int(rect.top),
                    width=width,
                    height=height,
                ))
            except Exception:
                pass
            return True

        # 回调对象必须在调用期间保持引用，否则会被 GC 掉
        callback = WNDENUMPROC(_collect)
        user32.EnumWindows(callback, 0)
        return results

    # ------------------------------------------------------------------ 截取
    def grab_window(self, win: WindowInfo) -> bytes:
        hwnd = wintypes.HWND(int(win.id))

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise CaptureError("读取窗口尺寸失败")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            raise CaptureError("窗口尺寸无效（可能已最小化）")

        hdc_window = user32.GetWindowDC(hwnd)
        if not hdc_window:
            raise CaptureError("获取窗口 DC 失败")
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        if not hdc_mem or not hbitmap:
            if hbitmap:
                gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)
            raise CaptureError("创建离屏位图失败")

        old_obj = gdi32.SelectObject(hdc_mem, hbitmap)
        try:
            printed = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
            data = self._read_pixels(hdc_mem, hbitmap, width, height)

            if not printed or _is_all_black(data):
                # PrintWindow 拿不到内容（GPU 合成窗口常见）时退回 BitBlt
                if gdi32.BitBlt(hdc_mem, 0, 0, width, height,
                                hdc_window, 0, 0, SRCCOPY):
                    data = self._read_pixels(hdc_mem, hbitmap, width, height)

            if _is_all_black(data):
                raise CaptureError(
                    "窗口内容为空（该窗口可能受保护、已最小化，"
                    "或被 GPU 独占渲染）"
                )
            return self._to_png(data, width, height)
        finally:
            gdi32.SelectObject(hdc_mem, old_obj)
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)

    def _read_pixels(self, hdc_mem, hbitmap, width: int, height: int) -> bytes:
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负值 = 自顶向下，省掉一次翻转
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        header.biSizeImage = width * height * 4

        buffer = ctypes.create_string_buffer(width * height * 4)
        scanned = gdi32.GetDIBits(
            hdc_mem, hbitmap, 0, height, buffer,
            ctypes.byref(header), DIB_RGB_COLORS,
        )
        if not scanned:
            raise CaptureError("读取窗口像素失败")
        return buffer.raw

    def _to_png(self, raw: bytes, width: int, height: int) -> bytes:
        # 这里用 Qt 做 PNG 编码，避免再引入 Pillow 之类的依赖。
        # GDI 给的是 BGRA，小端机上正好对应 QImage.Format_ARGB32。
        from PyQt5.QtCore import QBuffer, QIODevice
        from PyQt5.QtGui import QImage

        image = QImage(raw, width, height, width * 4, QImage.Format_ARGB32)
        if image.isNull():
            raise CaptureError("构造位图失败")
        image = image.copy()  # 必须深拷贝：raw 缓冲出了作用域就没了

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise CaptureError("PNG 编码失败")
        return bytes(buffer.data())

    # ------------------------------------------------------------------ 坐标
    def main_screen_width(self) -> int:
        return int(user32.GetSystemMetrics(SM_CXSCREEN))


def _is_all_black(data: bytes) -> bool:
    """整幅全 0 视为没抓到内容。``bytes.count`` 是 C 实现，够快。"""
    return bool(data) and data.count(0) == len(data)
