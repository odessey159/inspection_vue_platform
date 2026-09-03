"""Standalone point-cloud map catalog.

Maps live under ``.runtime/maps/<map_id>/`` and are independent of vehicles and
projects. Vehicles only store an optional ``map_id`` index. Catalog scenes are
pure point clouds: inspection trajectories are stored per vehicle and grown
from RTSP pose SEI, never baked into the map file.

Processing (voxel rebuild for PCD, layer compaction for scene.json) happens
after import.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..settings import MAPS_DIR
from .scene import build_scene_from_pcd
from .scene_transport import compact_scene_payload, load_compact_scene_json, write_compact_scene_json
from .storage import OFFLINE_WORKSPACE_ID, read_json, sanitize_workspace_key, write_json
from .vehicle_trajectory import overlay_vehicle_trajectory, strip_catalog_trajectory

MANIFEST_NAME = "manifest.json"
SCENE_NAME = "scene.json"


@dataclass(frozen=True)
class MapRecord:
    id: str
    name: str
    source_path: str
    source_kind: str
    source_fingerprint: str
    source_type: str
    raw_point_count: int
    render_point_count: int
    created_at: str
    processed_at: str

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "source_fingerprint": self.source_fingerprint,
            "source_type": self.source_type,
            "raw_point_count": self.raw_point_count,
            "render_point_count": self.render_point_count,
            "created_at": self.created_at,
            "processed_at": self.processed_at,
        }


def maps_root() -> Path:
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    return MAPS_DIR


def map_dir(map_id: str) -> Path:
    return maps_root() / sanitize_map_id(map_id)


def map_scene_path(map_id: str) -> Path:
    return map_dir(map_id) / SCENE_NAME


def map_manifest_path(map_id: str) -> Path:
    return map_dir(map_id) / MANIFEST_NAME


def sanitize_map_id(raw: str | None) -> str:
    cleaned = sanitize_workspace_key(raw)
    if cleaned == OFFLINE_WORKSPACE_ID:
        return "map"
    return cleaned


def resolve_map_source(path: Path) -> Path:
    path = path.resolve()
    if path.is_file() and path.suffix.lower() == ".pcd":
        return path
    if path.is_file() and path.name.lower() == "scene.json":
        return path
    if path.is_file() and path.suffix.lower() == ".json" and "scene" in path.stem.lower():
        return path
    if path.is_dir():
        candidate = path / "scene.json"
        if candidate.is_file():
            return candidate
        pcds = sorted(path.glob("*.pcd"))
        if len(pcds) == 1:
            return pcds[0]
        matches = sorted(path.glob("*scene*.json"))
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(f"Multiple scene JSON files found in {path}; pass a specific scene.json path")
    raise FileNotFoundError(f"Not a scene.json or PCD map source: {path}")


def is_map_import_source(path: Path) -> bool:
    try:
        resolve_map_source(path)
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


def list_maps() -> list[MapRecord]:
    records: list[MapRecord] = []
    root = maps_root()
    if not root.is_dir():
        return records
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        record = _read_record(child.name)
        if record is not None:
            records.append(record)
    return records


def get_map(map_id: str) -> MapRecord | None:
    cleaned = (map_id or "").strip()
    if not cleaned:
        return None
    return _read_record(cleaned)


def find_map_scene_path(map_id: str) -> Path | None:
    path = map_scene_path(map_id)
    return path if path.is_file() else None


def load_map_scene(map_id: str) -> dict:
    path = find_map_scene_path(map_id)
    if path is None:
        raise FileNotFoundError(f"Map '{map_id}' has no processed scene.json")
    payload = strip_catalog_trajectory(load_compact_scene_json(path))
    quality = payload.get("scene_quality")
    if not isinstance(quality, dict):
        quality = {}
    payload["scene_quality"] = {**quality, "map_id": map_id, "map_source": str(path)}
    return payload


def load_map_for_vehicle_id(vehicle_id: str | None) -> dict | None:
    """Resolve a vehicle's map_id index and load the catalog scene plus RTSP trajectory."""
    cleaned = (vehicle_id or "").strip()
    if not cleaned:
        return None
    from .rtsp_vehicles import get_vehicle_by_id, is_point_cloud_enabled

    if not is_point_cloud_enabled():
        return None
    vehicle = get_vehicle_by_id(cleaned)
    if vehicle is None or not vehicle.map_id:
        return None
    try:
        payload = load_map_scene(vehicle.map_id)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return overlay_vehicle_trajectory(payload, cleaned)


def import_map(
    source: Path,
    *,
    name: str | None = None,
    map_id: str | None = None,
    reuse_identical: bool = True,
) -> MapRecord:
    """Copy a scene.json/PCD into the catalog and process it for serving."""
    source_path = resolve_map_source(source)
    fingerprint = _source_fingerprint(source_path)
    if reuse_identical:
        existing = _find_by_fingerprint(fingerprint)
        if existing is not None:
            return existing

    kind = "pcd" if source_path.suffix.lower() == ".pcd" else "scene_json"
    preferred = map_id or name or source_path.stem
    allocated = _allocate_map_id(preferred)
    destination = map_dir(allocated)
    destination.mkdir(parents=True, exist_ok=True)
    scene_path = destination / SCENE_NAME

    if kind == "pcd":
        raw_payload = build_scene_from_pcd(source_path, scene_path)
        compact = compact_scene_payload(raw_payload)
    else:
        raw_payload = read_json(source_path)
        if not isinstance(raw_payload, dict):
            raise ValueError(f"Invalid scene.json payload: {source_path}")
        compact = compact_scene_payload(raw_payload)
    compact = strip_catalog_trajectory(compact)

    notes = list(compact.get("notes") or []) if isinstance(compact.get("notes"), list) else []
    process_note = "地图已作为独立资产导入并完成处理（压缩重复点云层）。"
    if process_note not in notes:
        notes.append(process_note)
    compact["notes"] = notes
    quality = compact.get("scene_quality")
    if not isinstance(quality, dict):
        quality = {}
    compact["scene_quality"] = {
        **quality,
        "map_id": allocated,
        "catalog": "standalone",
        "processed_after_import": True,
    }
    write_compact_scene_json(scene_path, compact)

    now = datetime.now(timezone.utc).isoformat()
    record = MapRecord(
        id=allocated,
        name=(name or "").strip() or source_path.stem,
        source_path=str(source_path),
        source_kind=kind,
        source_fingerprint=fingerprint,
        source_type=str(compact.get("source_type") or kind),
        raw_point_count=int(compact.get("raw_point_count") or compact.get("render_point_count") or 0),
        render_point_count=int(compact.get("render_point_count") or 0),
        created_at=now,
        processed_at=now,
    )
    write_json(map_manifest_path(allocated), record.payload())
    return record


def import_payload_as_map(payload: dict, *, name: str, map_id: str | None = None) -> MapRecord:
    """Process an in-memory scene dict into the catalog (e.g. rosbag rebuild)."""
    compact = strip_catalog_trajectory(compact_scene_payload(dict(payload)))
    allocated = _allocate_map_id(map_id or name)
    destination = map_dir(allocated)
    destination.mkdir(parents=True, exist_ok=True)
    scene_path = destination / SCENE_NAME
    notes = list(compact.get("notes") or []) if isinstance(compact.get("notes"), list) else []
    process_note = "地图已作为独立资产写入目录并完成处理。"
    if process_note not in notes:
        notes.append(process_note)
    compact["notes"] = notes
    write_compact_scene_json(scene_path, compact)
    now = datetime.now(timezone.utc).isoformat()
    record = MapRecord(
        id=allocated,
        name=name,
        source_path="",
        source_kind="scene_payload",
        source_fingerprint=f"payload:{allocated}",
        source_type=str(compact.get("source_type") or "lidar"),
        raw_point_count=int(compact.get("raw_point_count") or compact.get("render_point_count") or 0),
        render_point_count=int(compact.get("render_point_count") or 0),
        created_at=now,
        processed_at=now,
    )
    write_json(map_manifest_path(allocated), record.payload())
    return record


def migrate_legacy_vehicle_maps() -> list[MapRecord]:
    """Fold duplicated ``robots/<id>/maps/scene.json`` files into the catalog once."""
    from .rtsp_vehicles import load_rtsp_vehicles, robot_runtime_paths, update_vehicle_map_id

    imported: list[MapRecord] = []
    grouped: dict[str, list[tuple[str, Path]]] = {}
    for vehicle in load_rtsp_vehicles():
        if vehicle.map_id and get_map(vehicle.map_id) is not None:
            continue
        legacy = robot_runtime_paths(vehicle.id).maps / SCENE_NAME
        if not legacy.is_file():
            continue
        grouped.setdefault(_source_fingerprint(legacy), []).append((vehicle.id, legacy))

    for _fingerprint, items in grouped.items():
        source = items[0][1]
        record = import_map(source, name=source.parent.parent.name or "legacy-map")
        imported.append(record)
        for vehicle_id, _path in items:
            try:
                update_vehicle_map_id(vehicle_id, record.id)
            except (KeyError, ValueError):
                continue
    return imported


def _read_record(map_id: str) -> MapRecord | None:
    path = map_manifest_path(map_id)
    if not path.is_file():
        scene = map_scene_path(map_id)
        if not scene.is_file():
            return None
        return MapRecord(
            id=sanitize_map_id(map_id),
            name=map_id,
            source_path="",
            source_kind="scene_json",
            source_fingerprint="",
            source_type="unknown",
            raw_point_count=0,
            render_point_count=0,
            created_at="",
            processed_at="",
        )
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return MapRecord(
        id=str(payload.get("id") or map_id),
        name=str(payload.get("name") or map_id),
        source_path=str(payload.get("source_path") or ""),
        source_kind=str(payload.get("source_kind") or "scene_json"),
        source_fingerprint=str(payload.get("source_fingerprint") or ""),
        source_type=str(payload.get("source_type") or ""),
        raw_point_count=int(payload.get("raw_point_count") or 0),
        render_point_count=int(payload.get("render_point_count") or 0),
        created_at=str(payload.get("created_at") or ""),
        processed_at=str(payload.get("processed_at") or ""),
    )


def _allocate_map_id(preferred: str) -> str:
    base = sanitize_map_id(preferred)
    base = re.sub(r"[^a-zA-Z0-9._-]+", "-", base).strip("-._") or "map"
    candidate = base
    index = 2
    while map_manifest_path(candidate).is_file() or map_scene_path(candidate).is_file():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _find_by_fingerprint(fingerprint: str) -> MapRecord | None:
    if not fingerprint:
        return None
    for record in list_maps():
        if record.source_fingerprint == fingerprint:
            return record
    return None
