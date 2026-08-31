"""Per-vehicle inspection trajectory built from RTSP pose SEI.

Point-cloud maps stay pose-free. Each vehicle starts with an empty path; x/y/yaw
samples parsed from the livestream (or a finished recording) are appended so the
line grows as the robot drives.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .rtsp_sei import FrameMetadata, PictureMetadata, extract_picture_metadata_from_video
from .rtsp_vehicles import robot_runtime_paths
from .storage import write_json

TRAJECTORY_FILENAME = "trajectory.json"
TRAJECTORY_SOURCE = "rtsp_sei"
MIN_SAMPLE_INTERVAL_MS = 200
MIN_SAMPLE_DISTANCE_M = 0.15
STATIONARY_DISTANCE_M = 0.03
STATIONARY_HOLD_MS = 1200
MAX_TRAJECTORY_POINTS = 5000

_lock = threading.Lock()


@dataclass(frozen=True)
class _PoseSample:
    timestamp_ms: int
    x: float
    y: float
    z: float
    yaw: float


@dataclass
class VehicleTrajectory:
    vehicle_id: str
    source: str = TRAJECTORY_SOURCE
    trajectory: list[list[float]] = field(default_factory=list)
    trajectory_timestamps: list[int] = field(default_factory=list)
    trajectory_orientations: list[list[float]] = field(default_factory=list)
    updated_at: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "vehicle_id": self.vehicle_id,
            "source": self.source,
            "trajectory": self.trajectory,
            "trajectory_timestamps": self.trajectory_timestamps,
            "trajectory_orientations": self.trajectory_orientations,
            "point_count": len(self.trajectory),
            "updated_at": self.updated_at,
        }


def vehicle_trajectory_path(vehicle_id: str) -> Path:
    return robot_runtime_paths(vehicle_id).root / TRAJECTORY_FILENAME


def empty_vehicle_trajectory(vehicle_id: str) -> VehicleTrajectory:
    return VehicleTrajectory(vehicle_id=(vehicle_id or "").strip())


def load_vehicle_trajectory(vehicle_id: str) -> VehicleTrajectory:
    cleaned = (vehicle_id or "").strip()
    if not cleaned:
        return empty_vehicle_trajectory("")
    path = vehicle_trajectory_path(cleaned)
    if not path.is_file():
        return empty_vehicle_trajectory(cleaned)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return empty_vehicle_trajectory(cleaned)
    if not isinstance(raw, dict):
        return empty_vehicle_trajectory(cleaned)
    return _from_payload(cleaned, raw)


def strip_catalog_trajectory(payload: dict) -> dict:
    """Return a shallow copy of a map scene with no baked inspection path."""
    scene = dict(payload)
    scene["trajectory"] = []
    scene["trajectory_timestamps"] = []
    scene["trajectory_orientations"] = []
    quality = scene.get("scene_quality")
    if not isinstance(quality, dict):
        quality = {}
    scene["scene_quality"] = {
        **quality,
        "catalog_pure_map": True,
        "trajectory_source": None,
    }
    notes = list(scene.get("notes") or []) if isinstance(scene.get("notes"), list) else []
    note = "点云地图不含巡检轨迹；轨迹由各车 RTSP 位姿 SEI 生成并延长。"
    if note not in notes:
        notes.append(note)
    scene["notes"] = notes
    return scene


def overlay_vehicle_trajectory(payload: dict, vehicle_id: str | None) -> dict:
    """Replace any map-baked path with this vehicle's RTSP-grown trajectory."""
    scene = strip_catalog_trajectory(payload)
    cleaned = (vehicle_id or "").strip()
    if not cleaned:
        return scene
    record = load_vehicle_trajectory(cleaned)
    ground_z = _map_ground_z(scene)
    points = [_lift_point(point, ground_z) for point in record.trajectory]
    scene["trajectory"] = points
    scene["trajectory_timestamps"] = list(record.trajectory_timestamps)
    scene["trajectory_orientations"] = list(record.trajectory_orientations)
    quality = scene.get("scene_quality")
    if not isinstance(quality, dict):
        quality = {}
    scene["scene_quality"] = {
        **quality,
        "catalog_pure_map": True,
        "trajectory_source": record.source if points else None,
        "trajectory_point_count": len(points),
        "trajectory_vehicle_id": cleaned,
    }
    return scene


def backfill_vehicle_trajectories_from_recordings() -> int:
    """Grow each vehicle path from existing ``.frames.jsonl`` sidecars, if any."""
    from ..settings import RTSP_RECORDINGS_DIR
    from .rtsp_timeline import load_recording_frame_records
    from .rtsp_vehicles import load_rtsp_vehicles

    updated = 0
    root = RTSP_RECORDINGS_DIR
    if not root.is_dir():
        return 0
    for vehicle in load_rtsp_vehicles():
        folder = root / vehicle.id
        if not folder.is_dir():
            continue
        before = len(load_vehicle_trajectory(vehicle.id).trajectory)
        for video in sorted(folder.glob("recording_*.mp4")):
            pictures = load_recording_frame_records(video)
            if pictures:
                extend_vehicle_trajectory_from_pictures(vehicle.id, pictures)
        after = len(load_vehicle_trajectory(vehicle.id).trajectory)
        if after > before:
            updated += 1
    return updated


def extend_vehicle_trajectory_from_pictures(
    vehicle_id: str,
    pictures: tuple[PictureMetadata, ...] | list[PictureMetadata],
) -> VehicleTrajectory:
    samples = [_sample_from_picture(picture) for picture in pictures]
    return _extend_vehicle_trajectory(vehicle_id, [item for item in samples if item is not None])


def extend_vehicle_trajectory_from_pose(
    vehicle_id: str,
    *,
    timestamp_ms: int,
    x: float | None,
    y: float | None,
    yaw: float | None = None,
    z: float = 0.0,
) -> VehicleTrajectory | None:
    if x is None or y is None:
        return None
    sample = _PoseSample(
        timestamp_ms=int(timestamp_ms),
        x=float(x),
        y=float(y),
        z=float(z),
        yaw=0.0 if yaw is None else float(yaw),
    )
    return _extend_vehicle_trajectory(vehicle_id, [sample])


def extend_vehicle_trajectory_from_recording(
    vehicle_id: str,
    recording_path: Path,
    *,
    persist_sidecar: bool = False,
    timeout_sec: float | None = 12.0,
) -> VehicleTrajectory:
    cleaned = (vehicle_id or "").strip()
    if not cleaned or not recording_path.is_file():
        return empty_vehicle_trajectory(cleaned)
    try:
        pictures = extract_picture_metadata_from_video(recording_path, timeout_sec=timeout_sec)
    except (OSError, ValueError, TimeoutError):
        return load_vehicle_trajectory(cleaned)
    if persist_sidecar and pictures:
        from .rtsp_timeline import write_recording_frame_records

        write_recording_frame_records(recording_path, pictures)
    return extend_vehicle_trajectory_from_pictures(cleaned, pictures)


def _extend_vehicle_trajectory(vehicle_id: str, samples: list[_PoseSample]) -> VehicleTrajectory:
    cleaned = (vehicle_id or "").strip()
    if not cleaned:
        return empty_vehicle_trajectory("")
    with _lock:
        record = load_vehicle_trajectory(cleaned)
        changed = False
        for sample in samples:
            if _append_sample(record, sample):
                changed = True
        if len(record.trajectory) > MAX_TRAJECTORY_POINTS:
            _thin_in_place(record)
            changed = True
        if changed:
            record.updated_at = datetime.now(timezone.utc).isoformat()
            record.source = TRAJECTORY_SOURCE
            _write_record(record)
        return record


def _sample_from_picture(picture: PictureMetadata) -> _PoseSample | None:
    pose = picture.metadata
    if pose is None:
        return None
    return _sample_from_frame(pose)


def _sample_from_frame(pose: FrameMetadata) -> _PoseSample:
    return _PoseSample(
        timestamp_ms=int(pose.timestamp_ms),
        x=float(pose.x),
        y=float(pose.y),
        z=0.0,
        yaw=float(pose.yaw),
    )


def _append_sample(record: VehicleTrajectory, sample: _PoseSample) -> bool:
    if sample.timestamp_ms <= 0:
        return False
    if record.trajectory_timestamps:
        last_ts = record.trajectory_timestamps[-1]
        last = record.trajectory[-1]
        if sample.timestamp_ms < last_ts:
            return False
        if not _should_keep(last_ts, last[0], last[1], sample):
            if sample.timestamp_ms == last_ts:
                return False
            # Same place, newer time: refresh the last vertex so the live marker can move in time.
            if sample.timestamp_ms > last_ts and _xy_distance(last[0], last[1], sample.x, sample.y) < STATIONARY_DISTANCE_M:
                record.trajectory_timestamps[-1] = sample.timestamp_ms
                record.trajectory_orientations[-1] = _yaw_to_quaternion(sample.yaw)
                return True
            return False
    record.trajectory.append([round(sample.x, 4), round(sample.y, 4), round(sample.z, 4)])
    record.trajectory_timestamps.append(int(sample.timestamp_ms))
    record.trajectory_orientations.append(_yaw_to_quaternion(sample.yaw))
    return True


def _should_keep(last_ts: int, last_x: float, last_y: float, sample: _PoseSample) -> bool:
    dt = sample.timestamp_ms - last_ts
    dist = _xy_distance(last_x, last_y, sample.x, sample.y)
    if dist < STATIONARY_DISTANCE_M and dt < STATIONARY_HOLD_MS:
        return False
    if dt < MIN_SAMPLE_INTERVAL_MS and dist < MIN_SAMPLE_DISTANCE_M:
        return False
    return True


def _thin_in_place(record: VehicleTrajectory) -> None:
    count = len(record.trajectory)
    if count <= MAX_TRAJECTORY_POINTS:
        return
    step = max(1, math.ceil(count / MAX_TRAJECTORY_POINTS))
    keep = list(range(0, count - 1, step))
    if keep[-1] != count - 1:
        keep.append(count - 1)
    record.trajectory = [record.trajectory[index] for index in keep]
    record.trajectory_timestamps = [record.trajectory_timestamps[index] for index in keep]
    record.trajectory_orientations = [record.trajectory_orientations[index] for index in keep]


def _write_record(record: VehicleTrajectory) -> None:
    path = vehicle_trajectory_path(record.vehicle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, record.payload())


def _from_payload(vehicle_id: str, raw: dict) -> VehicleTrajectory:
    points_raw = raw.get("trajectory") or []
    timestamps_raw = raw.get("trajectory_timestamps") or []
    orientations_raw = raw.get("trajectory_orientations") or []
    points: list[list[float]] = []
    timestamps: list[int] = []
    orientations: list[list[float]] = []
    if not isinstance(points_raw, list) or not isinstance(timestamps_raw, list):
        return empty_vehicle_trajectory(vehicle_id)
    limit = min(len(points_raw), len(timestamps_raw))
    for index in range(limit):
        point = points_raw[index]
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            timestamps.append(int(timestamps_raw[index]))
            x = float(point[0])
            y = float(point[1])
            z = float(point[2]) if len(point) >= 3 else 0.0
        except (TypeError, ValueError):
            continue
        points.append([x, y, z])
        quat = [0.0, 0.0, 0.0, 1.0]
        if isinstance(orientations_raw, list) and index < len(orientations_raw):
            item = orientations_raw[index]
            if isinstance(item, list) and len(item) >= 4:
                try:
                    quat = [float(item[0]), float(item[1]), float(item[2]), float(item[3])]
                except (TypeError, ValueError):
                    quat = [0.0, 0.0, 0.0, 1.0]
        orientations.append(quat)
    return VehicleTrajectory(
        vehicle_id=vehicle_id,
        source=str(raw.get("source") or TRAJECTORY_SOURCE),
        trajectory=points,
        trajectory_timestamps=timestamps,
        trajectory_orientations=orientations,
        updated_at=str(raw.get("updated_at") or ""),
    )


def _yaw_to_quaternion(yaw: float) -> list[float]:
    half = float(yaw) * 0.5
    return [0.0, 0.0, round(math.sin(half), 6), round(math.cos(half), 6)]


def _xy_distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(bx - ax, by - ay)


def _map_ground_z(payload: dict) -> float:
    floor = payload.get("floor_cut_default")
    if isinstance(floor, (int, float)):
        return float(floor)
    for key in ("roof_removed_bounds", "bounds", "full_bounds"):
        bounds = payload.get(key)
        if not isinstance(bounds, dict):
            continue
        minimum = bounds.get("min")
        if isinstance(minimum, list) and len(minimum) >= 3:
            try:
                return float(minimum[2])
            except (TypeError, ValueError):
                continue
    return 0.0


def _lift_point(point: list[float], ground_z: float) -> list[float]:
    x = float(point[0])
    y = float(point[1])
    z = float(point[2]) if len(point) >= 3 else 0.0
    if abs(z) < 1e-6:
        z = ground_z
    return [x, y, z]
