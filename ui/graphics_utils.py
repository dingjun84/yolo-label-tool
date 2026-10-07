"""Shared helpers for QGraphicsItem selection."""

from PyQt5.QtCore import Qt, QPointF
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import QGraphicsItem, QGraphicsSimpleTextItem

BBOX_ROOT_TYPES = None

CLASS_LABEL_Z = 30
# 字号相对图像高度取值，缩放后视觉大小基本一致。
CLASS_LABEL_FONT_RATIO = 0.018
CLASS_LABEL_FONT_MIN = 9


def _bbox_root_types():
    global BBOX_ROOT_TYPES
    if BBOX_ROOT_TYPES is None:
        from ui.bbox_item import BBoxItem
        from ui.obb_item import OBBItem
        from ui.polygon_item import PolygonItem
        BBOX_ROOT_TYPES = (BBoxItem, OBBItem, PolygonItem)
    return BBOX_ROOT_TYPES


def resolve_bbox_root(item):
    """Walk up parent chain to BBoxItem/OBBItem/PolygonItem."""
    if item is None:
        return None
    bbox_roots = _bbox_root_types()
    current = item
    while current is not None:
        if isinstance(current, bbox_roots):
            return current
        current = current.parentItem()
    return None


def pick_preferred_bbox_root(selected_items):
    """When multiple items are selected, prefer highest z then largest bbox id."""
    roots = []
    seen = set()
    for item in selected_items:
        root = resolve_bbox_root(item)
        if root is not None and id(root) not in seen:
            seen.add(id(root))
            roots.append(root)
    if not roots:
        return None
    if len(roots) == 1:
        return roots[0]
    return max(
        roots,
        key=lambda r: (r.zValue(), r.bbox_data.id if r.bbox_data else -1),
    )


def select_only(item):
    """Clear scene selection then select this item (or its parent for handles)."""
    scene = item.scene()
    if scene is not None:
        scene.clearSelection()
    item.setSelected(True)


def select_only_parent(parent_item):
    """Clear scene selection then select the parent bbox item."""
    scene = parent_item.scene()
    if scene is not None:
        scene.clearSelection()
    parent_item.setSelected(True)


# ======================= 类别名标签 =======================

def class_label_font(image_rect) -> QFont:
    font = QFont()
    size = CLASS_LABEL_FONT_MIN
    if image_rect is not None and image_rect.height() > 0:
        size = max(CLASS_LABEL_FONT_MIN, int(image_rect.height() * CLASS_LABEL_FONT_RATIO))
    font.setPixelSize(size)
    font.setBold(True)
    return font


def class_label_pos(parent_item, anchor: QPointF, font: QFont, image_rect) -> QPointF:
    """默认放在框上方；上方没空间时放进框内顶部。anchor 用父项局部坐标。"""
    y = anchor.y() - font.pixelSize() - 2
    scene_y = parent_item.mapToScene(anchor).y() if parent_item is not None else anchor.y()
    if image_rect is not None and scene_y - font.pixelSize() - 2 < image_rect.top():
        y = anchor.y() + 2
    return QPointF(anchor.x(), y)


def create_class_label(parent_item, class_id, image_rect, anchor: QPointF) -> QGraphicsSimpleTextItem:
    from core.class_registry import class_label
    from ui.theme_manager import get_class_color

    text = QGraphicsSimpleTextItem(class_label(class_id), parent_item)
    text.setBrush(QBrush(QColor(get_class_color(class_id))))
    text.setFlag(QGraphicsItem.ItemIsSelectable, False)
    text.setAcceptedMouseButtons(Qt.NoButton)
    text.setZValue(CLASS_LABEL_Z)
    update_class_label(text, class_id, image_rect, anchor)
    return text


def update_class_label(text_item, class_id, image_rect, anchor: QPointF):
    from core.class_registry import class_label
    from ui.theme_manager import get_class_color

    font = class_label_font(image_rect)
    text_item.setText(class_label(class_id))
    text_item.setFont(font)
    text_item.setBrush(QBrush(QColor(get_class_color(class_id))))
    text_item.setPos(class_label_pos(text_item.parentItem(), anchor, font, image_rect))
