"""Load configured RTSP inspection vehicles from rtsp_vehicles.yaml.

Frontend edits are persisted to ``.runtime/rtsp_vehicles.override.yaml`` (writable in
Docker) and merged on top of the committed YAML so URL / map_id changes take
effect without editing backend source files.

Per-vehicle runtime dirs live under ``.runtime/robots/<id>/recordings``.
Point-cloud maps are a standalone catalog; vehicles only store an optional
``map_id`` index.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..settings import (
    POINT_CLOUD_ENABLED as _SETTINGS_POINT_CLOUD_ENABLED,
    ROBOT_MAPS_DIRNAME,
    ROBOT_RECORDINGS_DIRNAME,
    ROBOTS_DIR,
    RTSP_VEHICLES_OVERRIDE_PATH,
    RTSP_VEHICLES_PATH,
)
from .rtsp_recorder import DEFAULT_RTSP_URL

_point_cloud_override: bool | None = None
_vehicles_file_lock = threading.Lock()


def is_point_cloud_enabled() -> bool:
    """Runtime-toggleable flag; defaults to ``POINT_CLOUD_ENABLED`` env."""
    if _point_cloud_override is not None:
        return _point_cloud_override
    return _SETTINGS_POINT_CLOUD_ENABLED


def set_point_cloud_enabled(enabled: bool) -> bool:
    global _point_cloud_override
    _point_cloud_override = enabled
    return is_point_cloud_enabled()


def point_cloud_settings_payload() -> dict[str, bool]:
    return {"point_cloud_enabled": is_point_cloud_enabled()}


@dataclass(frozen=True)
class RtspVehicle:
    id: str
    name: str
    rtsp_url: str
    map_id: str | None = None


@dataclass(frozen=True)
class RobotRuntimePaths:
    root: Path
    recordings: Path
    maps: Path


def load_rtsp_vehicles() -> list[RtspVehicle]:
    """Return vehicles from YAML plus runtime URL / map_id overrides."""
    with _vehicles_file_lock:
        return _load_rtsp_vehicles_unlocked()


def _load_rtsp_vehicles_unlocked() -> list[RtspVehicle]:
    base = _parse_vehicles_file(RTSP_VEHICLES_PATH)
    if not base:
        base = [_default_vehicle()]
    overrides = _parse_override_records(RTSP_VEHICLES_OVERRIDE_PATH)
    merged: list[RtspVehicle] = []
    for vehicle in base:
        override = overrides.get(vehicle.id, {})
        url = str(override.get("rtsp_url") or vehicle.rtsp_url).strip() or vehicle.rtsp_url
        if "map_id" in override:
            raw_map = override.get("map_id")
            map_id = str(raw_map).strip() if raw_map else None
        else:
            map_id = vehicle.map_id
        merged.append(RtspVehicle(id=vehicle.id, name=vehicle.name, rtsp_url=url, map_id=map_id))
    return merged


def update_vehicle_rtsp_url(vehicle_id: str, rtsp_url: str) -> RtspVehicle:
    """Persist a new RTSP URL for an existing vehicle and return the updated record."""
    cleaned_id = vehicle_id.strip()
    cleaned_url = rtsp_url.strip()
    if not cleaned_id or cleaned_id in {".", ".."} or "/" in cleaned_id or "\\" in cleaned_id:
        raise ValueError("Invalid vehicle id")
    if not cleaned_url.lower().startswith("rtsp://"):
        raise ValueError("RTSP URL must start with rtsp://")

    with _vehicles_file_lock:
        vehicles = _load_rtsp_vehicles_unlocked()
        current = next((item for item in vehicles if item.id == cleaned_id), None)
        if current is None:
            raise KeyError(f"Unknown RTSP vehicle id: {cleaned_id}")

        base = _parse_vehicles_file(RTSP_VEHICLES_PATH)
        base_url = next((item.rtsp_url for item in base if item.id == cleaned_id), None)
        _upsert_override(cleaned_id, rtsp_url=cleaned_url, base_url=base_url)
        updated = RtspVehicle(id=current.id, name=current.name, rtsp_url=cleaned_url, map_id=current.map_id)

    ensure_robot_runtime_dirs([updated])
    return updated


def update_vehicle_map_id(vehicle_id: str, map_id: str | None) -> RtspVehicle:
    """Persist the vehicle's map index (None clears the binding)."""
    cleaned_id = vehicle_id.strip()
    cleaned_map = (map_id or "").strip() or None
    if not cleaned_id or cleaned_id in {".", ".."} or "/" in cleaned_id or "\\" in cleaned_id:
        raise ValueError("Invalid vehicle id")
    if cleaned_map:
        from .maps import get_map

        if get_map(cleaned_map) is None:
            raise ValueError(f"Unknown map id: {cleaned_map}")

    with _vehicles_file_lock:
        vehicles = _load_rtsp_vehicles_unlocked()
        current = next((item for item in vehicles if item.id == cleaned_id), None)
        if current is None:
            raise KeyError(f"Unknown RTSP vehicle id: {cleaned_id}")

        base = _parse_vehicles_file(RTSP_VEHICLES_PATH)
        base_vehicle = next((item for item in base if item.id == cleaned_id), None)
        base_map_id = base_vehicle.map_id if base_vehicle is not None else None
        _upsert_override(
            cleaned_id,
            map_id=cleaned_map,
            map_id_explicit=True,
            base_map_id=base_map_id,
        )
        updated = RtspVehicle(id=current.id, name=current.name, rtsp_url=current.rtsp_url, map_id=cleaned_map)

    return updated


def _upsert_override(
    vehicle_id: str,
    *,
    rtsp_url: str | None = None,
    base_url: str | None = None,
    map_id: str | None = None,
    map_id_explicit: bool = False,
    base_map_id: str | None = None,
) -> None:
    records = _parse_override_records(RTSP_VEHICLES_OVERRIDE_PATH)
    current = dict(records.get(vehicle_id) or {})

    if rtsp_url is not None:
        if base_url and _normalize_url(base_url) == _normalize_url(rtsp_url):
            current.pop("rtsp_url", None)
        else:
            current["rtsp_url"] = rtsp_url

    if map_id_explicit:
        if (base_map_id or None) == (map_id or None):
            current.pop("map_id", None)
        else:
            current["map_id"] = map_id

    if current:
        records[vehicle_id] = current
    else:
        records.pop(vehicle_id, None)
    _write_override_records(records)


def _write_override_records(records: dict[str, dict]) -> None:
    path = RTSP_VEHICLES_OVERRIDE_PATH
    if not records:
        if path.exists():
            path.unlink()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    vehicles_payload: list[dict[str, object]] = []
    for item_id, fields in records.items():
        entry: dict[str, object] = {"id": item_id}
        if fields.get("rtsp_url"):
            entry["rtsp_url"] = fields["rtsp_url"]
        if "map_id" in fields:
            entry["map_id"] = fields.get("map_id")
        vehicles_payload.append(entry)
    payload = {"vehicles": vehicles_payload}
    text = (
        "# Frontend/runtime RTSP URL and map_id overrides. Merged on top of backend/config/rtsp_vehicles.yaml.\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )
    tmp_path = path.with_suffix(".yaml.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _parse_vehicles_file(path: Path) -> list[RtspVehicle]:
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    raw_items = payload.get("vehicles") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []

    vehicles: list[RtspVehicle] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        vehicle_id = str(item.get("id") or f"vehicle-{index + 1}").strip()
        name = str(item.get("name") or vehicle_id).strip()
        rtsp_url = str(item.get("rtsp_url") or "").strip()
        raw_map = item.get("map_id")
        map_id = str(raw_map).strip() if raw_map else None
        if not vehicle_id or not name or not rtsp_url.lower().startswith("rtsp://"):
            continue
        if vehicle_id in seen_ids:
            continue
        seen_ids.add(vehicle_id)
        vehicles.append(RtspVehicle(id=vehicle_id, name=name, rtsp_url=rtsp_url, map_id=map_id))
    return vehicles


def _parse_override_records(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    raw_items = payload.get("vehicles") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return {}
    records: dict[str, dict] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        vehicle_id = str(item.get("id") or "").strip()
        if not vehicle_id:
            continue
        entry: dict = {}
        url = str(item.get("rtsp_url") or "").strip()
        if url.lower().startswith("rtsp://"):
            entry["rtsp_url"] = url
        if "map_id" in item:
            raw_map = item.get("map_id")
            entry["map_id"] = str(raw_map).strip() if raw_map else None
        if entry:
            records[vehicle_id] = entry
    return records


def _parse_override_file(path: Path) -> list[RtspVehicle]:
    """Backward-compatible helper used by older tests."""
    vehicles: list[RtspVehicle] = []
    for item in _parse_vehicles_file(path):
        vehicles.append(RtspVehicle(id=item.id, name=item.name or item.id, rtsp_url=item.rtsp_url, map_id=item.map_id))
    return vehicles


def _normalize_url(rtsp_url: str) -> str:
    return rtsp_url.strip().rstrip("/").lower()


def _default_vehicle() -> RtspVehicle:
    return RtspVehicle(
        id="default",
        name="默认巡检车",
        rtsp_url=DEFAULT_RTSP_URL,
        map_id=None,
    )


def robot_runtime_paths(vehicle_id: str) -> RobotRuntimePaths:
    """Per-vehicle runtime dirs under .runtime/robots/<id>/{recordings,maps}."""
    root = ROBOTS_DIR / vehicle_id
    return RobotRuntimePaths(
        root=root,
        recordings=root / ROBOT_RECORDINGS_DIRNAME,
        maps=root / ROBOT_MAPS_DIRNAME,
    )


def ensure_robot_runtime_dirs(vehicles: list[RtspVehicle] | None = None) -> list[RobotRuntimePaths]:
    """Create robots/<vehicle_id>/recordings for each configured vehicle.

    Legacy ``maps/`` folders are left in place for one-time catalog migration
    but are no longer created or used as the map store.
    """
    ROBOTS_DIR.mkdir(parents=True, exist_ok=True)
    resolved: list[RobotRuntimePaths] = []
    for vehicle in vehicles if vehicles is not None else load_rtsp_vehicles():
        paths = robot_runtime_paths(vehicle.id)
        paths.recordings.mkdir(parents=True, exist_ok=True)
        resolved.append(paths)
    return resolved


def find_robot_map_scene(vehicle_id: str) -> Path | None:
    """Resolve the catalog scene for a vehicle's map_id index."""
    cleaned = (vehicle_id or "").strip()
    if not cleaned:
        return None
    vehicle = get_vehicle_by_id(cleaned)
    if vehicle is None or not vehicle.map_id:
        return None
    from .maps import find_map_scene_path

    return find_map_scene_path(vehicle.map_id)


def get_vehicle_by_id(vehicle_id: str) -> RtspVehicle | None:
    cleaned = vehicle_id.strip()
    if not cleaned:
        return None
    for vehicle in load_rtsp_vehicles():
        if vehicle.id == cleaned:
            return vehicle
    return None


def rtsp_vehicle_payloads() -> list[dict[str, str | None]]:
    return [
        {
            "id": vehicle.id,
            "name": vehicle.name,
            "rtsp_url": vehicle.rtsp_url,
            "map_id": vehicle.map_id,
        }
        for vehicle in load_rtsp_vehicles()
    ]
