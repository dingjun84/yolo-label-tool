
"""Regression: switching images must not inflate YOLO w/h via resize handles."""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication, QGraphicsScene
from PyQt5.QtCore import QRectF

from core.bbox import BBox
from ui.bbox_item import BBoxItem


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class BBoxSyncTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.scene = QGraphicsScene()
        self.img = QRectF(0, 0, 1000, 800)
        self.bbox = BBox(
            id=0,
            class_id=0,
            type="rect",
            x_center=0.5,
            y_center=0.5,
            width=0.2,
            height=0.1,
        )
        pixel_w = self.bbox.width * self.img.width()
        pixel_h = self.bbox.height * self.img.height()
        x = self.bbox.x_center * self.img.width() - pixel_w / 2
        y = self.bbox.y_center * self.img.height() - pixel_h / 2
        item = BBoxItem(QRectF(0, 0, pixel_w, pixel_h), self.bbox)
        item.setPos(x, y)
        item.set_image_rect(self.img)
        self.scene.addItem(item)
        self.item = item

    def test_set_image_rect_does_not_mutate_yolo(self):
        self.assertAlmostEqual(self.bbox.width, 0.2)
        self.assertAlmostEqual(self.bbox.height, 0.1)
        self.assertAlmostEqual(self.bbox.x_center, 0.5)
        self.assertAlmostEqual(self.bbox.y_center, 0.5)

    def test_sync_excludes_handles(self):
        self.item._sync_to_yolo()
        self.assertAlmostEqual(self.bbox.width, 0.2, places=6)
        self.assertAlmostEqual(self.bbox.height, 0.1, places=6)
        # sceneBoundingRect includes pen/handles; that must not leak into YOLO
        inflated = self.item.sceneBoundingRect()
        box = self.item.scene_box_rect()
        self.assertGreater(inflated.width(), box.width())

    def test_repeated_rebuild_does_not_grow(self):
        for _ in range(20):
            self.item.set_image_rect(self.img)
            self.item._sync_to_yolo()
        self.assertAlmostEqual(self.bbox.width, 0.2, places=6)
        self.assertAlmostEqual(self.bbox.height, 0.1, places=6)


if __name__ == "__main__":
    unittest.main()
