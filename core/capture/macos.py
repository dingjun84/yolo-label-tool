"""macOS 窗口捕获后端（CoreGraphics / PyObjC）。

依赖 ``pyobjc-framework-Quartz``。窗口枚举与命中测试用 ``CGWindowListCopyWindowInfo``，
抓图优先走进程内的 ``CGWindowListCreateImage``，失败再退回系统自带的
``screencapture -l <windowid>``（后者对某些 GPU 合成窗口更稳）。

坐标是 CoreGraphics 的全局显示坐标（逻辑点，原点在主显示器左上），
与 Qt 在 macOS 上的坐标语义一致，所以覆盖层的换算系数通常是 1.0。

窗口层：只取 ``kCGWindowLayer == 0`` 的普通窗口。Qt 的 ``Qt::Tool``（截图遮罩自己）
和弹出菜单分别落在 8 / 101 层，所以遮罩天然不会被列进来；``own_exclude`` 只是
再上一道保险。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import (
    MIN_WINDOW_SIZE,
    CaptureError,
    CaptureUnavailable,
    WindowBackend,
    WindowInfo,
)

_IMPORT_ERROR = None
try:
    import Quartz
    from Foundation import NSMutableData
except Exception as exc:  # pragma: no cover - 仅在缺依赖时触发
    Quartz = None
    NSMutableData = None
    _IMPORT_ERROR = exc

INSTALL_HINT = (
    "缺少 pyobjc-framework-Quartz，窗口截图需要它。\n\n"
    "安装命令（在本项目的 conda 环境里执行）：\n"
    "pip install pyobjc-framework-Quartz"
)


def _get(container, key, default=None):
    """兼容 PyObjC 的 NSDictionary 与 Python dict 两种访问方式。"""
    if container is None:
        return default
    try:
        value = container[key]
    except Exception:
        value = None
    if value is None:
        try:
            value = container.get(key)
        except Exception:
            value = None
    return default if value is None else value


def _const(*names, default=0):
    """取 Quartz 里的常量。

    PyObjC 各版本导出的名字不完全一致 —— 例如「排除桌面元素」在 12.x 里叫
    ``kCGWindowListExcludeDesktopElements``，比头文件少了 ``Option`` 一词。
    这里按候选名依次尝试，最后回落到 CoreGraphics 头文件里的数值定义。
    """
    for name in names:
        value = getattr(Quartz, name, None)
        if value is not None:
            return value
    return default


def _bounds_of(info):
    """从窗口信息里取出 (x, y, w, h)，取不到返回 None。"""
    bounds = _get(info, "kCGWindowBounds")
    if bounds is None:
        return None
    try:
        return (
            int(round(float(_get(bounds, "X", 0)))),
            int(round(float(_get(bounds, "Y", 0)))),
            int(round(float(_get(bounds, "Width", 0)))),
            int(round(float(_get(bounds, "Height", 0)))),
        )
    except (TypeError, ValueError):
        return None


class MacWindowBackend(WindowBackend):
    name = "macos-coregraphics"

    # ------------------------------------------------------------------ 可用性
    def available(self):
        if Quartz is None:
            return False, f"{INSTALL_HINT}\n\n原始错误：{_IMPORT_ERROR}"
        try:
            granted = Quartz.CGPreflightScreenCaptureAccess()
        except AttributeError:
            granted = True  # 老系统没有这个 API，交给后续抓图去报错
        if not granted:
            return False, (
                "未获得「屏幕录制」权限，无法截取窗口内容。\n\n"
                "请到 系统设置 → 隐私与安全性 → 屏幕录制，勾选本程序"
                "（从终端启动时请勾选「终端」或 iTerm），然后重启本程序。"
            )
        return True, ""

    # ------------------------------------------------------------------ 枚举
    def list_windows(self, own_exclude=None):
        if Quartz is None:
            raise CaptureUnavailable(INSTALL_HINT)

        options = (
            _const("kCGWindowListOptionOnScreenOnly", default=1 << 0)
            | _const("kCGWindowListOptionExcludeDesktopElements",
                     "kCGWindowListExcludeDesktopElements", default=1 << 4)
        )
        infos = Quartz.CGWindowListCopyWindowInfo(
            options, Quartz.kCGNullWindowID
        ) or []

        my_pid = os.getpid()
        windows = []
        for info in infos:
            try:
                number = int(_get(info, "kCGWindowNumber", 0))

                # 本进程的窗口：只排除截图遮罩自己，其余（主窗口弹出来的
                # 设置框 / 消息框等）都要能选。own_exclude 为 None 时退回
                # 保守策略 —— 整个进程的窗口都不列出。
                if int(_get(info, "kCGWindowOwnerPID", -1)) == my_pid:
                    if own_exclude is None or number in own_exclude:
                        continue

                # 只要普通窗口层，跳过菜单栏/悬浮窗/Dock 之类
                if int(_get(info, "kCGWindowLayer", 0)) != 0:
                    continue
                if float(_get(info, "kCGWindowAlpha", 1.0)) < 0.05:
                    continue

                rect = _bounds_of(info)
                if rect is None:
                    continue
                x, y, width, height = rect
                if width < MIN_WINDOW_SIZE or height < MIN_WINDOW_SIZE:
                    continue

                windows.append(WindowInfo(
                    id=number,
                    title=str(_get(info, "kCGWindowName", "") or ""),
                    app=str(_get(info, "kCGWindowOwnerName", "") or ""),
                    x=x, y=y, width=width, height=height,
                ))
            except Exception:
                continue  # 单条异常不该让整次枚举失败

        return windows

    # ------------------------------------------------------------------ 截取
    def grab_window(self, win: WindowInfo) -> bytes:
        if Quartz is None:
            raise CaptureUnavailable(INSTALL_HINT)

        png = self._grab_via_coregraphics(win)
        if png:
            return png
        return self._grab_via_screencapture(win)

    def _grab_via_coregraphics(self, win: WindowInfo):
        try:
            rect = Quartz.CGRectMake(win.x, win.y, win.width, win.height)
            image_option = (
                _const("kCGWindowImageBoundsIgnoreFraming", default=1 << 0)
                # 取物理分辨率（Retina 下是 2x），更接近训练图的像素尺度
                | _const("kCGWindowImageBestResolution", default=1 << 3)
            )

            image = Quartz.CGWindowListCreateImage(
                rect,
                _const("kCGWindowListOptionIncludingWindow", default=1 << 3),
                win.id,
                image_option,
            )
            if image is None:
                return None
            return self._cgimage_to_png(image)
        except Exception:
            return None

    def _cgimage_to_png(self, image):
        try:
            data = NSMutableData.data()
            dest = Quartz.CGImageDestinationCreateWithData(
                data, "public.png", 1, None
            )
            if dest is None:
                return None
            Quartz.CGImageDestinationAddImage(dest, image, None)
            if not Quartz.CGImageDestinationFinalize(dest):
                return None
            return bytes(data)
        except Exception:
            return None

    def _grab_via_screencapture(self, win: WindowInfo) -> bytes:
        """回退方案：系统自带的 screencapture，用 -l 指定窗口号。"""
        if not win.id:
            raise CaptureError("拿不到窗口编号，无法截图")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "shot.png"
            proc = subprocess.run(
                ["screencapture", "-x", "-o", "-t", "png",
                 "-l", str(win.id), str(out)],
                capture_output=True,
            )
            if proc.returncode != 0 or not out.is_file():
                detail = proc.stderr.decode("utf-8", "ignore").strip()
                raise CaptureError(
                    "截取窗口失败"
                    + (f"：{detail}" if detail else "")
                    + "（常见原因是缺少「屏幕录制」权限，或该窗口受保护）"
                )
            return out.read_bytes()

    # ------------------------------------------------------------------ 坐标
    def main_screen_width(self) -> int:
        if Quartz is None:
            raise CaptureUnavailable(INSTALL_HINT)
        rect = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return int(round(rect.size.width))
