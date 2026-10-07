"""固定类别表：微信 / 企业微信界面截图标注用。

resources/classes.txt 中的行顺序即 class id（0 开始），
该 id 会被写入 YOLO txt。文件缺失时回退到内置清单。
"""

from pathlib import Path

CLASSES_FILE = Path(__file__).resolve().parent.parent / "resources" / "classes.txt"

DEFAULT_CLASS_NAMES = (
    "self_avatar",
    "nav_chat_icon",
    "nav_contacts_icon",
    "search_bar",
    "list_item",
    "send_button",
    "incoming_bubble",
    "outgoing_bubble",
    "input_bar",
    "single_chat",
    "group_chat",
    "contact_send_message",
    "nav_groups_icon",
)

_cache = None


def _read_class_names():
    try:
        lines = CLASSES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return list(DEFAULT_CLASS_NAMES)
    names = [line.strip() for line in lines]
    names = [name for name in names if name and not name.startswith("#")]
    return names or list(DEFAULT_CLASS_NAMES)


def get_class_names():
    global _cache
    if _cache is None:
        _cache = _read_class_names()
    return _cache


def get_class_name(class_id) -> str:
    names = get_class_names()
    if 0 <= class_id < len(names):
        return names[class_id]
    return f"class_{class_id}"


def class_label(class_id) -> str:
    """下拉框 / 列表里显示的文本，例如 "[4] contact_item"。"""
    return f"[{class_id}] {get_class_name(class_id)}"