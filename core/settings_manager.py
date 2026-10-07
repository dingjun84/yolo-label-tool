from dataclasses import dataclass
from enum import Enum
from typing import Optional

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtGui import QKeySequence

ORG = "YOLOTxtMaker"
APP = "YOLOTxtMaker"

KEY_AUTO_SAVE_ON_NAV = "auto_save_on_nav"
KEY_LANGUAGE = "language"
KEY_PERIODIC_AUTO_SAVE = "periodic_auto_save"
KEY_PERIODIC_INTERVAL_MIN = "periodic_interval_min"
KEY_SAVE_FOLDER = "save_folder_path"
KEY_LAST_IMAGE_DIR = "last_image_dir"
KEY_LAST_FOLDER = "last_folder_path"
KEY_YOLO_API = "yolo_api_url"
KEY_YOLO_CONF = "yolo_conf"
KEY_YOLO_IOU = "yolo_iou"
KEY_YOLO_IMGSZ = "yolo_imgsz"

DEFAULT_AUTO_SAVE_ON_NAV = True
DEFAULT_LANGUAGE = "zh"
DEFAULT_PERIODIC_AUTO_SAVE = False
DEFAULT_PERIODIC_INTERVAL_MIN = 5
# 模型预标注服务（yolo26/server.py 的 /predict）
DEFAULT_YOLO_API = "http://192.168.1.13:8080"
DEFAULT_YOLO_CONF = 0.25
DEFAULT_YOLO_IOU = 0.7
# 推理分辨率必须与训练一致（该权重按 imgsz=1280 训练），不要为了省时间往下调
DEFAULT_YOLO_IMGSZ = 1280

VALID_LANGUAGES = ("zh", "en", "ja")


class ShortcutKey(str, Enum):
    RECT = "shortcut_rect"
    OBB = "shortcut_obb"
    POLYGON = "shortcut_polygon"
    POLYGON_FINISH = "shortcut_polygon_finish"
    POLYGON_CANCEL = "shortcut_polygon_cancel"
    SAVE = "shortcut_save"
    UNDO = "shortcut_undo"
    ADD = "shortcut_add"
    DELETE = "shortcut_delete"
    PREV_IMAGE = "shortcut_prev_image"
    NEXT_IMAGE = "shortcut_next_image"


DEFAULT_SHORTCUTS = {
    ShortcutKey.RECT: "R",
    ShortcutKey.OBB: "O",
    ShortcutKey.POLYGON: "P",
    ShortcutKey.POLYGON_FINISH: "Return",
    ShortcutKey.POLYGON_CANCEL: "Escape",
    ShortcutKey.SAVE: "Ctrl+S",
    ShortcutKey.UNDO: "Ctrl+Z",
    ShortcutKey.ADD: "A",
    ShortcutKey.DELETE: "Delete",
    ShortcutKey.PREV_IMAGE: "Left",
    ShortcutKey.NEXT_IMAGE: "Right",
}


@dataclass
class AppSettings:
    auto_save_on_nav: bool = DEFAULT_AUTO_SAVE_ON_NAV
    language: str = DEFAULT_LANGUAGE
    periodic_auto_save: bool = DEFAULT_PERIODIC_AUTO_SAVE
    periodic_interval_min: int = DEFAULT_PERIODIC_INTERVAL_MIN
    shortcuts: dict = None
    yolo_api_url: str = DEFAULT_YOLO_API
    yolo_conf: float = DEFAULT_YOLO_CONF
    yolo_iou: float = DEFAULT_YOLO_IOU
    yolo_imgsz: int = DEFAULT_YOLO_IMGSZ

    def __post_init__(self):
        if self.shortcuts is None:
            self.shortcuts = default_shortcuts()


def _settings() -> QSettings:
    return QSettings(ORG, APP)


def default_shortcuts() -> dict:
    return {k.value: v for k, v in DEFAULT_SHORTCUTS.items()}


def load_all() -> AppSettings:
    s = _settings()
    shortcuts = default_shortcuts()
    for key in ShortcutKey:
        val = s.value(key.value, shortcuts[key.value])
        shortcuts[key.value] = str(val) if val else shortcuts[key.value]

    lang = str(s.value(KEY_LANGUAGE, DEFAULT_LANGUAGE))
    if lang not in VALID_LANGUAGES:
        lang = DEFAULT_LANGUAGE

    try:
        interval = int(s.value(KEY_PERIODIC_INTERVAL_MIN, DEFAULT_PERIODIC_INTERVAL_MIN))
    except (TypeError, ValueError):
        interval = DEFAULT_PERIODIC_INTERVAL_MIN
    interval = max(1, min(60, interval))

    api_url = str(s.value(KEY_YOLO_API, DEFAULT_YOLO_API) or "").strip()
    if not api_url:
        api_url = DEFAULT_YOLO_API

    try:
        yolo_imgsz = int(s.value(KEY_YOLO_IMGSZ, DEFAULT_YOLO_IMGSZ))
    except (TypeError, ValueError):
        yolo_imgsz = DEFAULT_YOLO_IMGSZ
    yolo_imgsz = max(64, min(1920, yolo_imgsz))

    return AppSettings(
        auto_save_on_nav=_read_bool(s, KEY_AUTO_SAVE_ON_NAV, DEFAULT_AUTO_SAVE_ON_NAV),
        language=lang,
        periodic_auto_save=_read_bool(s, KEY_PERIODIC_AUTO_SAVE, DEFAULT_PERIODIC_AUTO_SAVE),
        periodic_interval_min=interval,
        shortcuts=shortcuts,
        yolo_api_url=api_url,
        yolo_conf=_read_float(s, KEY_YOLO_CONF, DEFAULT_YOLO_CONF, 0.0, 1.0),
        yolo_iou=_read_float(s, KEY_YOLO_IOU, DEFAULT_YOLO_IOU, 0.0, 1.0),
        yolo_imgsz=yolo_imgsz,
    )


def _read_bool(settings: QSettings, key: str, default: bool) -> bool:
    v = settings.value(key, default, type=bool)
    if isinstance(v, bool):
        return v
    return _to_bool(v, default)


def _to_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes")


def _read_float(settings: QSettings, key: str, default: float,
                low: float, high: float) -> float:
    """读浮点配置并夹到 [low, high]；坏值回退默认值。"""
    try:
        v = float(settings.value(key, default))
    except (TypeError, ValueError):
        v = default
    if v != v:  # NaN
        v = default
    return min(max(v, low), high)


def load_language() -> str:
    return load_all().language


def _valid_dir(path_str) -> Optional[str]:
    if not path_str:
        return None
    p = str(path_str).strip()
    if not p:
        return None
    from pathlib import Path
    path = Path(p)
    if path.is_dir():
        return str(path)
    return None


def load_path_prefs() -> dict:
    s = _settings()
    return {
        "save_folder": _valid_dir(s.value(KEY_SAVE_FOLDER)),
        "last_image_dir": _valid_dir(s.value(KEY_LAST_IMAGE_DIR)),
        "last_folder": _valid_dir(s.value(KEY_LAST_FOLDER)),
    }


def save_path_pref(key: str, value: str):
    s = _settings()
    s.setValue(key, value)
    s.sync()


def save_settings(settings: AppSettings):
    s = _settings()
    s.setValue(KEY_AUTO_SAVE_ON_NAV, bool(settings.auto_save_on_nav))
    s.setValue(KEY_LANGUAGE, settings.language)
    s.setValue(KEY_PERIODIC_AUTO_SAVE, bool(settings.periodic_auto_save))
    s.setValue(KEY_PERIODIC_INTERVAL_MIN, int(settings.periodic_interval_min))
    for key in ShortcutKey:
        s.setValue(key.value, settings.shortcuts.get(key.value, DEFAULT_SHORTCUTS[key]))
    s.setValue(KEY_YOLO_API, settings.yolo_api_url)
    s.setValue(KEY_YOLO_CONF, float(settings.yolo_conf))
    s.setValue(KEY_YOLO_IOU, float(settings.yolo_iou))
    s.setValue(KEY_YOLO_IMGSZ, int(settings.yolo_imgsz))
    s.sync()


def save_shortcuts(shortcuts: dict):
    s = _settings()
    for key in ShortcutKey:
        s.setValue(key.value, shortcuts.get(key.value, DEFAULT_SHORTCUTS[key]))


def get_shortcut(settings: AppSettings, key: ShortcutKey) -> str:
    return settings.shortcuts.get(key.value, DEFAULT_SHORTCUTS[key])


def _normalize_shortcut_for_conflict(shortcut_str: str) -> str:
    s = shortcut_str.strip().lower()
    if s in ("return", "enter"):
        return "return"
    return s


def key_event_matches(event, shortcut_str: str) -> bool:
    """Match a QKeyEvent against a shortcut string (PyQt5-safe)."""
    if not shortcut_str or not str(shortcut_str).strip():
        return False
    expected = QKeySequence(str(shortcut_str).strip())
    pressed = QKeySequence(int(event.modifiers()) | event.key())
    if expected.matches(pressed) == QKeySequence.ExactMatch:
        return True
    if event.key() in (Qt.Key_Return, Qt.Key_Enter) and expected.count() == 1:
        for alt in ("Return", "Enter"):
            if QKeySequence(alt).matches(expected) == QKeySequence.ExactMatch:
                return (event.modifiers() & ~Qt.KeypadModifier) == Qt.NoModifier
    return False


def shortcuts_conflict(shortcuts: dict) -> bool:
    values = [shortcuts.get(k.value, DEFAULT_SHORTCUTS[k]) for k in ShortcutKey]
    normalized = [
        _normalize_shortcut_for_conflict(v) for v in values if v and str(v).strip()
    ]
    return len(normalized) != len(set(normalized))
