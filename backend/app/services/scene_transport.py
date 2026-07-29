"""Compact large scene.json payloads for fast API transport.

Used by project scene APIs and vehicle onboard map preview. Drops duplicated
point layers down to a single ``render_points`` copy and caches ``scene.web.json``
beside the source map so repeated loads skip multi-hundred-MB JSON parses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import read_json

POINT_ARRAY_KEYS = (
    "points",
    "full_points",
    "roof_removed_points",
    "floor_removed_points",
    "structure_points",
    "render_points",
)

# Prefer layers the frontend already falls back through for display.
_PRIMARY_PREF = (
    "render_points",
    "structure_points",
    "roof_removed_points",
    "points",
    "full_points",
    "floor_removed_points",
)

WEB_SCENE_FILENAME = "scene.web.json"


def select_primary_points(payload: dict[str, Any]) -> list[Any]:
    for key in _PRIMARY_PREF:
        points = payload.get(key)
        if isinstance(points, list) and points:
            return points
    return []


def compact_scene_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Drop duplicated point layers so JSON ships ~1 copy instead of 6.

    Onboard maps often store the same 600k points under points / full_points /
    roof_removed / floor_removed / structure / render. The viewer can fall back
    to ``render_points`` for both full-height and cut modes.
    """
    primary = select_primary_points(payload)
    if not primary:
        return payload

    populated = 0
    for key in POINT_ARRAY_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value:
            populated += 1
    # Already compact (single layer) — avoid copying large arrays unnecessarily.
    if populated <= 1 and isinstance(payload.get("render_points"), list) and payload.get("render_points"):
        return payload

    compact = dict(payload)
    for key in POINT_ARRAY_KEYS:
        compact[key] = []
    compact["render_points"] = primary
    # Keep a lightweight alias used by some stats paths without a second JSON copy:
    # structure_points stays empty; counts still reflect the display layer.
    count = len(primary)
    compact["render_point_count"] = count
    compact["structure_point_count"] = int(payload.get("structure_point_count") or count)
    compact["raw_point_count"] = int(payload.get("raw_point_count") or count)
    quality = compact.get("scene_quality")
    if not isinstance(quality, dict):
        quality = {}
        compact["scene_quality"] = quality
    quality = {**quality, "transport": "compact_single_layer", "transport_point_count": count}
    compact["scene_quality"] = quality
    notes = list(compact.get("notes") or []) if isinstance(compact.get("notes"), list) else []
    note = "已压缩重复点云层以便快速加载（保留 render_points 供前端显示）。"
    if note not in notes:
        notes = [*notes, note]
    compact["notes"] = notes
    return compact


def web_scene_cache_path(map_path: Path) -> Path:
    return map_path.with_name(WEB_SCENE_FILENAME)


def write_compact_scene_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def load_compact_scene_json(map_path: Path, *, use_cache: bool = True) -> dict[str, Any]:
    """
    Load a map scene and return a transport-compact payload.

    Caches ``scene.web.json`` beside the source so the second request skips the
    multi-hundred-MB parse of duplicated layers.
    """
    if not map_path.is_file():
        raise FileNotFoundError(f"Scene map not found: {map_path}")

    cache_path = web_scene_cache_path(map_path)
    if use_cache and cache_path.is_file():
        try:
            if cache_path.stat().st_mtime >= map_path.stat().st_mtime:
                cached = read_json(cache_path)
                if isinstance(cached, dict) and select_primary_points(cached):
                    return cached
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    payload = read_json(map_path)
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid scene map JSON: {map_path}")
    compact = compact_scene_payload(payload)
    if use_cache:
        try:
            write_compact_scene_json(cache_path, compact)
        except OSError:
            pass
    return compact
