from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from yolo_service.frame_layout import (
    LAYOUT_FULL,
    LAYOUT_QUAD,
    resolve_frame_layout,
    split_quad_tiles,
)


class _FakeFrame:
    def __init__(self, height: int, width: int) -> None:
        self.shape = (height, width, 3)

    def __getitem__(self, key):
        y_key, x_key = key[0], key[1]
        y0, y1, _ = y_key.indices(self.shape[0])
        x0, x1, _ = x_key.indices(self.shape[1])
        return _FakeFrame(y1 - y0, x1 - x0)


class YoloFrameLayoutTest(unittest.TestCase):
    def test_resolve_frame_layout_aliases(self) -> None:
        self.assertEqual(resolve_frame_layout("quad"), LAYOUT_QUAD)
        self.assertEqual(resolve_frame_layout("2x2"), LAYOUT_QUAD)
        self.assertEqual(resolve_frame_layout("full"), LAYOUT_FULL)
        self.assertEqual(resolve_frame_layout("whole"), LAYOUT_FULL)
        self.assertEqual(resolve_frame_layout(None, default="quad"), LAYOUT_QUAD)
        self.assertEqual(resolve_frame_layout("unknown", default="full"), LAYOUT_FULL)

    def test_split_quad_tiles_geometry_and_labels(self) -> None:
        tiles = split_quad_tiles(_FakeFrame(40, 80))
        self.assertEqual(len(tiles), 4)
        labels = [item[0] for item in tiles]
        origins = [(item[2], item[3]) for item in tiles]
        sizes = [item[1].shape[:2] for item in tiles]
        self.assertEqual(labels, ["front", "rear", "left", "right"])
        self.assertEqual(origins, [(0, 0), (40, 0), (0, 20), (40, 20)])
        self.assertEqual(sizes, [(20, 40), (20, 40), (20, 40), (20, 40)])

    def test_split_quad_tiles_uses_custom_labels(self) -> None:
        with patch.dict("os.environ", {"YOLO_QUAD_TILE_LABELS": "a,b,c,d"}):
            tiles = split_quad_tiles(_FakeFrame(10, 10))
        self.assertEqual([item[0] for item in tiles], ["a", "b", "c", "d"])

    def test_split_quad_tiles_rejects_tiny_frames(self) -> None:
        self.assertEqual(split_quad_tiles(object()), [])
        self.assertEqual(split_quad_tiles(_FakeFrame(1, 10)), [])


if __name__ == "__main__":
    unittest.main()
