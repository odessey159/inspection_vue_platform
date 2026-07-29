"""Load configured RTSP inspection vehicles from rtsp_vehicles.yaml.

Also manages per-vehicle runtime dirs under ``.runtime/robots/<id>/`` for
recordings and onboard ``maps/scene.json`` preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..settings import (
    POINT_CLOUD_ENABLED as _SETTINGS_POINT_CLOUD_ENABLED,
    ROBOT_MAPS_DIRNAME,
    ROBOT_RECORDINGS_DIRNAME,
    ROBOTS_DIR,
    RTSP_VEHICLES_PATH,
)
from .rtsp_recorder import DEFAULT_RTSP_URL

_point_cloud_override: bool | None = None


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


@dataclass(frozen=True)
class RobotRuntimePaths:
    root: Path
    recordings: Path
    maps: Path


def load_rtsp_vehicles() -> list[RtspVehicle]:
    """Return vehicles from YAML, or a single default entry when the file is missing."""
    path = RTSP_VEHICLES_PATH
    if not path.exists():
        return [_default_vehicle()]

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_items = payload.get("vehicles") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return [_default_vehicle()]

    vehicles: list[RtspVehicle] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        vehicle_id = str(item.get("id") or f"vehicle-{index + 1}").strip()
        name = str(item.get("name") or vehicle_id).strip()
        rtsp_url = str(item.get("rtsp_url") or "").strip()
        if not vehicle_id or not name or not rtsp_url.lower().startswith("rtsp://"):
            continue
        if vehicle_id in seen_ids:
            continue
        seen_ids.add(vehicle_id)
        vehicles.append(RtspVehicle(id=vehicle_id, name=name, rtsp_url=rtsp_url))

    return vehicles or [_default_vehicle()]


def _default_vehicle() -> RtspVehicle:
    return RtspVehicle(
        id="default",
        name="默认巡检车",
        rtsp_url=DEFAULT_RTSP_URL,
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
    """
    Create robots/<vehicle_id>/{recordings,maps} for each configured vehicle.

    Does not migrate or delete existing .runtime/rtsp_recordings content.
    """
    ROBOTS_DIR.mkdir(parents=True, exist_ok=True)
    resolved: list[RobotRuntimePaths] = []
    for vehicle in vehicles if vehicles is not None else load_rtsp_vehicles():
        paths = robot_runtime_paths(vehicle.id)
        paths.recordings.mkdir(parents=True, exist_ok=True)
        paths.maps.mkdir(parents=True, exist_ok=True)
        resolved.append(paths)
    return resolved


def find_robot_map_scene(vehicle_id: str) -> Path | None:
    """Return robots/<id>/maps/scene.json when present."""
    cleaned = vehicle_id.strip()
    if not cleaned:
        return None
    scene_path = robot_runtime_paths(cleaned).maps / "scene.json"
    if scene_path.is_file():
        return scene_path
    return None


def get_vehicle_by_id(vehicle_id: str) -> RtspVehicle | None:
    cleaned = vehicle_id.strip()
    if not cleaned:
        return None
    for vehicle in load_rtsp_vehicles():
        if vehicle.id == cleaned:
            return vehicle
    return None


def rtsp_vehicle_payloads() -> list[dict[str, str]]:
    return [
        {
            "id": vehicle.id,
            "name": vehicle.name,
            "rtsp_url": vehicle.rtsp_url,
        }
        for vehicle in load_rtsp_vehicles()
    ]
