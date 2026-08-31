"""Frame layout for YOLO: full-frame (legacy) or 2x2 mosaic tiles.

The live inspection stream is a 2x2 mosaic (top-left / top-right / bottom-left /
bottom-right). Quad mode crops each decoded picture into those four tiles and
runs YOLO on each crop separately. Full mode keeps the previous whole-frame
inference path.

Tile order is always::

    [0] top-left     [1] top-right
    [2] bottom-left  [3] bottom-right

Labels default to front, rear, left, right (matching the local test publisher).
Override with ``YOLO_QUAD_TILE_LABELS``.
"""

from __future__ import annotations

import os
from typing import Any

LAYOUT_FULL = "full"
LAYOUT_QUAD = "quad"
DEFAULT_FRAME_LAYOUT = LAYOUT_QUAD
DEFAULT_QUAD_TILE_LABELS = ("front", "rear", "left", "right")


def resolve_frame_layout(value: str | None, *, default: str = DEFAULT_FRAME_LAYOUT) -> str:
    raw = (value if value is not None else default) or default
    normalized = str(raw).strip().lower()
    if normalized in {"full", "whole", "none", "off", "single"}:
        return LAYOUT_FULL
    if normalized in {"quad", "2x2", "mosaic", "tiles", "quadrant"}:
        return LAYOUT_QUAD
    fallback = str(default).strip().lower()
    return LAYOUT_FULL if fallback in {"full", "whole", "none", "off", "single"} else LAYOUT_QUAD


def env_frame_layout() -> str:
    return resolve_frame_layout(os.getenv("YOLO_FRAME_LAYOUT"), default=DEFAULT_FRAME_LAYOUT)


def quad_tile_labels() -> tuple[str, str, str, str]:
    raw = os.getenv("YOLO_QUAD_TILE_LABELS", "").strip()
    parts = [item.strip() for item in raw.split(",") if item.strip()] if raw else []
    if len(parts) != 4:
        return DEFAULT_QUAD_TILE_LABELS
    return (parts[0], parts[1], parts[2], parts[3])


def split_quad_tiles(frame: Any) -> list[tuple[str, Any, int, int]]:
    """Return ``(label, crop, origin_x, origin_y)`` for a 2x2 mosaic frame."""
    shape = getattr(frame, "shape", None)
    if shape is None or len(shape) < 2:
        return []
    height = int(shape[0])
    width = int(shape[1])
    if height < 2 or width < 2:
        return []
    mid_y = height // 2
    mid_x = width // 2
    labels = quad_tile_labels()
    return [
        (labels[0], frame[0:mid_y, 0:mid_x], 0, 0),
        (labels[1], frame[0:mid_y, mid_x:width], mid_x, 0),
        (labels[2], frame[mid_y:height, 0:mid_x], 0, mid_y),
        (labels[3], frame[mid_y:height, mid_x:width], mid_x, mid_y),
    ]
