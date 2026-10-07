#!/usr/bin/env python3
"""模型预标注：调用 yolo26 的 ``/predict``，把检测结果写成 ``<图片名>_model.txt``。

设计要点
--------
- **只写 ``<stem>_model.txt``，绝不碰人工标注的 ``<stem>.txt``**（两者都放在
  label-tool 的「保存路径」目录下）。两者的差别不只是「模型预测」与「人工答案」：
  ``_model.txt`` 存不存在，同时也是 label-tool 判断「这张图人工校正过了没有」的
  状态位 —— 人工保存 ``<stem>.txt`` 时会把 ``<stem>_model.txt`` 删掉。
- 已有人工标注的图片默认**跳过**，否则重跑预标注会把已完成的图重新打回「待校正」。
- 网络异常、解码失败、服务报错都不中断整批，逐张记录，最后由 Report 汇总。

本模块不依赖 PyQt，可单独跑（便于在没有 GUI 的环境里验证）。
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
MODEL_STEM_SUFFIX = "_model"
DEFAULT_TIMEOUT = 60.0

# 进度回调：(已完成张数, 总张数, 图片路径, 状态)
# 状态取值见 Report 的 *_STATUS 常量，文案由 UI 层负责翻译。
ProgressFn = Callable[[int, int, Path, str], None]
CancelFn = Callable[[], bool]

STATUS_LABELED = "labeled"   # 已生成预标注
STATUS_EMPTY = "empty"       # 调用成功但没检出任何目标
STATUS_SKIPPED = "skipped"   # 已有人工标注，跳过
STATUS_FAILED = "failed"     # 调用或写盘失败


class AutolabelError(RuntimeError):
    """预标注过程中可预期的错误（配置错、连不上、响应异常等）。"""


# --------------------------------------------------------------------------
# 路径工具
# --------------------------------------------------------------------------
def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def _label_dir(image_path: Path, save_folder: Optional[Path] = None) -> Path:
    """标注 txt 所在目录：统一跟随 label-tool 的「保存路径」。

    保存路径为空时退回图片所在目录，保证任何情况下都能算出一个确定位置；
    正常情况下 label-tool 会先要求设置保存路径。
    """
    return Path(save_folder) if save_folder else image_path.parent


def model_label_path(image_path: Path, save_folder: Optional[Path] = None) -> Path:
    """模型预标注的落盘位置：``<保存路径>/<stem>_model.txt``。"""
    return _label_dir(image_path, save_folder) / (image_path.stem + MODEL_STEM_SUFFIX + ".txt")


def human_label_path(image_path: Path, save_folder: Optional[Path] = None) -> Path:
    """人工标注的 txt：``<保存路径>/<stem>.txt``，与 label-tool 原有机制一致。"""
    return _label_dir(image_path, save_folder) / (image_path.stem + ".txt")


def has_model_prediction(image_path: Path, save_folder: Optional[Path] = None) -> bool:
    """是否存在模型预标注（不管内容是否为空）。"""
    try:
        return model_label_path(image_path, save_folder).is_file()
    except OSError:
        return False


def has_human_label(image_path: Path, save_folder: Optional[Path] = None) -> bool:
    """是否存在**非空**的人工标注。

    空文件视为「没有标注」，与 label-tool 里 ``_has_labeled_txt`` 的判定一致。
    """
    p = human_label_path(image_path, save_folder)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def load_model_labels(image_path: Path, save_folder: Optional[Path] = None):
    """按 YOLO txt 读回模型预标注（复用 core.yolo_io）。

    供 label-tool 在 ``<stem>.txt`` 不存在时回退显示模型预测用。
    """
    from core.yolo_io import load_yolo_txt

    return load_yolo_txt(model_label_path(image_path, save_folder))


def normalize_api_url(url: str) -> str:
    """规范化服务地址：补 scheme、去尾部斜杠、容忍用户填到具体接口。

    ``http://192.168.1.13:8080/`` -> ``http://192.168.1.13:8080``
    """
    u = (url or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    u = u.rstrip("/")
    for suffix in ("/predict/image", "/predict", "/classes", "/health", "/ocr"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u.rstrip("/")


# --------------------------------------------------------------------------
# 结果结构
# --------------------------------------------------------------------------
@dataclass
class ImageResult:
    image: Path
    status: str
    n_boxes: int = 0
    written: Optional[Path] = None
    error: str = ""


@dataclass
class Report:
    total: int = 0
    labeled: int = 0
    empty: int = 0
    skipped: int = 0
    failed: int = 0
    aborted: bool = False
    results: list = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.labeled + self.empty + self.skipped + self.failed

    @property
    def clean(self) -> bool:
        return self.failed == 0 and not self.aborted

    def summary(self) -> str:
        parts = [
            f"共 {self.total} 张",
            f"生成 {self.labeled}",
        ]
        if self.empty:
            parts.append(f"其中无检出 {self.empty}")
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.aborted:
            parts.append("已中断")
        return "，".join(parts)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _post_predict(api_url: str, image_bytes: bytes, conf: float, iou: float,
                  imgsz: int, timeout: float) -> dict:
    """POST /predict（JSON + base64 传图），返回解析后的响应体。"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = json.dumps({"image_base64": b64}).encode("utf-8")
    qs = urllib.parse.urlencode({
        "conf": float(conf),
        "iou": float(iou),
        "imgsz": int(imgsz),
    })
    req = urllib.request.Request(
        f"{api_url}/predict?{qs}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        raise AutolabelError(
            f"服务返回 HTTP {e.code}" + (f"：{detail}" if detail else "")
        ) from e
    except urllib.error.URLError as e:
        raise AutolabelError(f"无法连接预标注服务（{e.reason}）") from e
    except socket.timeout as e:
        raise AutolabelError(f"请求超时（>{timeout:g}s）") from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AutolabelError("响应不是合法 JSON") from e

    if not isinstance(data, dict) or not data.get("success", True):
        msg = data.get("error") if isinstance(data, dict) else None
        raise AutolabelError(msg or "服务返回 success=false")
    return data


def detections_to_lines(data: dict) -> list:
    """把 /predict 的响应转成 YOLO 行（``class cx cy w h``，全部归一化）。"""
    img = data.get("image") or {}
    try:
        width = float(img.get("width") or 0)
        height = float(img.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0.0
    if width <= 0 or height <= 0:
        raise AutolabelError("响应里缺少图片尺寸，无法归一化坐标")

    lines = []
    for det in data.get("detections") or []:
        try:
            cid = int(det["class_id"])
            cx, cy, bw, bh = (float(v) for v in det["xywh"])
        except (KeyError, TypeError, ValueError):
            continue  # 单条脏数据跳过，不拖累整张图
        lines.append(
            f"{cid} {_clamp01(cx / width):.6f} {_clamp01(cy / height):.6f} "
            f"{_clamp01(bw / width):.6f} {_clamp01(bh / height):.6f}"
        )
    return lines


def _write_lines(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def collect_images(folder: Path) -> list:
    """列出目录内可标注的图片（一层，不递归），按文件名排序。"""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(f for f in folder.iterdir() if f.is_file() and is_image_file(f))


def autolabel_folder(
    images: Iterable,
    api_url: str,
    conf: float = 0.25,
    iou: float = 0.7,
    imgsz: int = 1280,
    timeout: float = DEFAULT_TIMEOUT,
    skip_labeled: bool = True,
    save_folder: Optional[Path] = None,
    on_progress: Optional[ProgressFn] = None,
    should_cancel: Optional[CancelFn] = None,
) -> Report:
    """对一批图片跑预标注。

    Args:
        images: 图片路径列表。
        api_url: 服务地址，任何形态都行（会走 normalize_api_url）。
        skip_labeled: 已有非空人工标注的图片是否跳过。默认 True ——
            否则重跑预标注会给已完成的图重新生成 ``_model.txt``，
            把它的状态打回「待校正」。
        save_folder: 标注 txt 所在目录（label-tool 的「保存路径」）。
        on_progress: 每张完成后的回调。
        should_cancel: 返回 True 时中断（已处理的保留）。

    Returns:
        Report
    """
    api = normalize_api_url(api_url)
    if not api:
        raise AutolabelError("未配置预标注服务地址（设置 → 模型预标注）")

    images = [Path(p) for p in images]
    report = Report(total=len(images))

    for index, img in enumerate(images, start=1):
        if should_cancel and should_cancel():
            report.aborted = True
            break

        if skip_labeled and has_human_label(img, save_folder):
            report.skipped += 1
            report.results.append(
                ImageResult(img, STATUS_SKIPPED, error="已有人工标注，跳过")
            )
            if on_progress:
                on_progress(index, report.total, img, STATUS_SKIPPED)
            continue

        try:
            data = _post_predict(api, img.read_bytes(), conf, iou, imgsz, timeout)
            lines = detections_to_lines(data)
            out = model_label_path(img, save_folder)
            _write_lines(out, lines)
            status = STATUS_LABELED if lines else STATUS_EMPTY
            report.labeled += 1
            if not lines:
                report.empty += 1
            report.results.append(
                ImageResult(img, status, n_boxes=len(lines), written=out)
            )
        except AutolabelError as e:
            report.failed += 1
            report.results.append(ImageResult(img, STATUS_FAILED, error=str(e)))
        except OSError as e:
            report.failed += 1
            report.results.append(
                ImageResult(img, STATUS_FAILED, error=f"读写失败：{e}")
            )
        except Exception as e:  # 兜底：单张图的意外不该毁掉整批
            report.failed += 1
            report.results.append(
                ImageResult(img, STATUS_FAILED, error=f"{type(e).__name__}: {e}")
            )

        if on_progress:
            on_progress(index, report.total, img, report.results[-1].status)

    return report


def check_service(api_url: str, timeout: float = 5.0) -> dict:
    """探测服务是否可用，返回 /health 的内容。失败抛 AutolabelError。"""
    api = normalize_api_url(api_url)
    if not api:
        raise AutolabelError("未配置预标注服务地址")
    try:
        with urllib.request.urlopen(f"{api}/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise AutolabelError(f"无法连接预标注服务（{e.reason}）") from e
    except socket.timeout as e:
        raise AutolabelError(f"连接超时（>{timeout:g}s）") from e
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AutolabelError("健康检查响应不是合法 JSON") from e
    if not isinstance(data, dict):
        raise AutolabelError("健康检查响应格式异常")
    return data
