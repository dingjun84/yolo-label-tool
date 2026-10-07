"""窗口捕获后端：按平台分派实现。

- **macOS**：CoreGraphics（`pyobjc-framework-Quartz`）枚举窗口 + 抓窗口像素
- **Windows**：`user32` / `gdi32`，纯 ctypes，不引入第三方依赖

统一契约
--------
- ``list_windows()`` / ``hit_test()`` 的坐标是**后端自己的屏幕坐标系**
  （macOS 是逻辑点，Windows 是物理像素）。覆盖层用 ``main_screen_width()``
  与 Qt 主屏宽度之比换算到 Qt 逻辑坐标，见 ``ui/capture_overlay.py``。
- ``grab_window()`` 一律返回 **PNG 字节**（原始分辨率，Retina 下是物理像素）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional, Tuple

# 小于该边长的窗口不参与吸附（过滤工具条、悬浮块、输入法候选框之类）
MIN_WINDOW_SIZE = 80


class CaptureError(RuntimeError):
    """窗口捕获失败（含权限、编码、系统调用失败）。"""


class CaptureUnavailable(CaptureError):
    """当前平台或环境不支持窗口捕获。"""


@dataclass(frozen=True)
class WindowInfo:
    """一个可截图的顶层窗口。"""

    id: int
    title: str
    app: str
    x: int
    y: int
    width: int
    height: int

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    @property
    def area(self) -> int:
        return self.width * self.height

    def contains(self, px: float, py: float) -> bool:
        return (self.x <= px < self.x + self.width
                and self.y <= py < self.y + self.height)

    def display_name(self) -> str:
        if self.title and self.app:
            return f"{self.app} — {self.title}"
        return self.title or self.app or f"窗口 {self.id}"


class WindowBackend:
    """窗口捕获后端接口。"""

    name = "base"

    def available(self) -> Tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。原因会直接展示给用户。"""
        raise NotImplementedError

    def list_windows(self) -> list:
        """按 z 序（最前 -> 最后）列出可吸附的窗口。"""
        raise NotImplementedError

    def grab_window(self, win: WindowInfo) -> bytes:
        """抓取指定窗口，返回 PNG 字节。"""
        raise NotImplementedError

    def main_screen_width(self) -> int:
        """主屏宽度，用**本后端坐标系**表示（用于换算到 Qt 逻辑坐标）。"""
        raise NotImplementedError

    def hit_test(self, px: float, py: float) -> Optional[WindowInfo]:
        """返回坐标点命中的窗口。

        ``list_windows`` 已按 z 序排列，因此第一个命中的就是视觉上最上层的那个 ——
        比按面积挑更贴近用户的直觉。
        """
        for win in self.list_windows():
            if win.contains(px, py):
                return win
        return None


def create_backend() -> WindowBackend:
    """按当前平台创建后端。不支持时抛 CaptureUnavailable。"""
    if sys.platform == "darwin":
        from .macos import MacWindowBackend

        return MacWindowBackend()
    if sys.platform.startswith("win"):
        from .windows import WindowsWindowBackend

        return WindowsWindowBackend()
    raise CaptureUnavailable(f"暂不支持当前平台：{sys.platform}")
