from __future__ import annotations

import bisect
import csv
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from ..settings import SCENE_MAX_TRAJECTORY_SAMPLES


@dataclass
class PairFrame:
    image_timestamp_ms: int
    point_timestamp_ms: int
    image_path: Path
    point_path: Path


@dataclass
class PoseRecord:
    timestamp_ms: int
    x: float
    y: float
    z: float
    qx: float
    qy: float
    qz: float
    qw: float
    yaw: float


@dataclass(frozen=True)
class InterpolatedPose:
    pose: PoseRecord
    source_gap_ms: int
    interpolated: bool


class PoseTimeline:
    def __init__(self, poses: list[PoseRecord]):
        if not poses:
            raise ValueError("Cannot build a pose timeline without pose samples")
        self.poses = sorted(poses, key=lambda item: item.timestamp_ms)
        self.timestamps = [pose.timestamp_ms for pose in self.poses]

    @property
    def start_ms(self) -> int:
        return self.timestamps[0]

    @property
    def end_ms(self) -> int:
        return self.timestamps[-1]

    @property
    def max_gap_ms(self) -> int:
        if len(self.timestamps) < 2:
            return 0
        return max(b - a for a, b in zip(self.timestamps, self.timestamps[1:]))

    def at(self, target_ms: int) -> InterpolatedPose | None:
        if target_ms < self.start_ms or target_ms > self.end_ms:
            return None

        index = bisect.bisect_left(self.timestamps, target_ms)
        if index < len(self.timestamps) and self.timestamps[index] == target_ms:
            return InterpolatedPose(pose=self.poses[index], source_gap_ms=0, interpolated=False)
        if index <= 0 or index >= len(self.timestamps):
            return None

        before = self.poses[index - 1]
        after = self.poses[index]
        gap = after.timestamp_ms - before.timestamp_ms
        if gap <= 0:
            return InterpolatedPose(pose=before, source_gap_ms=0, interpolated=False)
        ratio = (target_ms - before.timestamp_ms) / gap
        return InterpolatedPose(
            pose=_interpolate_pose(before, after, target_ms, ratio),
            source_gap_ms=gap,
            interpolated=True,
        )


def parse_pairs_csv(pairs_csv_path: Path) -> list[PairFrame]:
    if not pairs_csv_path.exists():
        raise FileNotFoundError(f"Pairs CSV not found: {pairs_csv_path}")
    root = pairs_csv_path.parent
    pairs: list[PairFrame] = []
    with pairs_csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            image_path = root / Path(str(row["image_path"]).replace("\\", "/"))
            point_path = root / Path(str(row["pointcloud_path"]).replace("\\", "/"))
            pairs.append(
                PairFrame(
                    image_timestamp_ms=_ns_to_ms(row["image_timestamp_ns"]),
                    point_timestamp_ms=_ns_to_ms(row["lidar_timestamp_ns"]),
                    image_path=image_path,
                    point_path=point_path,
                )
            )
    if not pairs:
        raise ValueError(f"No pairs found in {pairs_csv_path}")
    pairs.sort(key=lambda item: item.image_timestamp_ms)
    return pairs


def parse_pose_csv(path: Path) -> list[PoseRecord]:
    if not path.exists():
        raise FileNotFoundError(f"Pose CSV not found: {path}")
    poses: list[PoseRecord] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp_ms = _resolve_pose_timestamp_ms(row)
            poses.append(
                PoseRecord(
                    timestamp_ms=timestamp_ms,
                    x=float(row["x"]),
                    y=float(row["y"]),
                    z=float(row["z"]),
                    qx=float(row["qx"]),
                    qy=float(row["qy"]),
                    qz=float(row["qz"]),
                    qw=float(row["qw"]),
                    yaw=float(row.get("yaw_rad", "0") or 0),
                )
            )
    if not poses:
        raise ValueError(f"No pose rows found in {path}")
    poses.sort(key=lambda item: item.timestamp_ms)
    return poses


def build_dataset_summary(pairs: list[PairFrame]) -> dict[str, int | float]:
    image_ts = [pair.image_timestamp_ms for pair in pairs]
    point_ts = [pair.point_timestamp_ms for pair in pairs]
    offsets = [pair.point_timestamp_ms - pair.image_timestamp_ms for pair in pairs]
    median_gap_ms = _median_diff(image_ts)
    inferred_fps = round(1000 / median_gap_ms, 3) if median_gap_ms else 1.0
    return {
        "video_start_ts": image_ts[0],
        "video_end_ts": image_ts[-1],
        "point_start_ts": point_ts[0],
        "point_end_ts": point_ts[-1],
        "median_video_gap_ms": median_gap_ms,
        "median_point_gap_ms": _median_diff(point_ts),
        "time_offset_ms": int(statistics.median(offsets)) if offsets else 0,
        "inferred_fps": inferred_fps,
        "frame_count": len(pairs),
    }


def _sample_trajectory_poses(poses: list[PoseRecord], max_samples: int = SCENE_MAX_TRAJECTORY_SAMPLES) -> list[PoseRecord]:
    if not poses:
        return []
    if len(poses) <= max_samples:
        return poses
    indices = sorted({round(index * (len(poses) - 1) / (max_samples - 1)) for index in range(max_samples)})
    return [poses[index] for index in indices]


def sample_trajectory(poses: list[PoseRecord], max_samples: int = SCENE_MAX_TRAJECTORY_SAMPLES) -> tuple[list[list[float]], list[int]]:
    chosen = _sample_trajectory_poses(poses, max_samples)
    return [[round(item.x, 4), round(item.y, 4), round(item.z, 4)] for item in chosen], [item.timestamp_ms for item in chosen]


def sample_trajectory_with_orientations(
    poses: list[PoseRecord],
    max_samples: int = SCENE_MAX_TRAJECTORY_SAMPLES,
) -> tuple[list[list[float]], list[int], list[list[float]]]:
    chosen = _sample_trajectory_poses(poses, max_samples)
    trajectory = [[round(item.x, 4), round(item.y, 4), round(item.z, 4)] for item in chosen]
    timestamps = [item.timestamp_ms for item in chosen]
    orientations = [
        [round(item.qx, 6), round(item.qy, 6), round(item.qz, 6), round(item.qw, 6)]
        for item in chosen
    ]
    return trajectory, timestamps, orientations


def nearest_pose(poses: list[PoseRecord], target_ms: int) -> PoseRecord | None:
    if not poses:
        return None
    return min(poses, key=lambda item: abs(item.timestamp_ms - target_ms))


def _interpolate_pose(before: PoseRecord, after: PoseRecord, target_ms: int, ratio: float) -> PoseRecord:
    ratio = max(0.0, min(1.0, ratio))
    qx, qy, qz, qw = _normalized_quaternion_lerp(
        (before.qx, before.qy, before.qz, before.qw),
        (after.qx, after.qy, after.qz, after.qw),
        ratio,
    )
    yaw_delta = ((after.yaw - before.yaw + math.pi) % (2 * math.pi)) - math.pi
    return PoseRecord(
        timestamp_ms=target_ms,
        x=before.x + (after.x - before.x) * ratio,
        y=before.y + (after.y - before.y) * ratio,
        z=before.z + (after.z - before.z) * ratio,
        qx=qx,
        qy=qy,
        qz=qz,
        qw=qw,
        yaw=before.yaw + yaw_delta * ratio,
    )


def _normalized_quaternion_lerp(
    before: tuple[float, float, float, float],
    after: tuple[float, float, float, float],
    ratio: float,
) -> tuple[float, float, float, float]:
    ax, ay, az, aw = before
    bx, by, bz, bw = after
    dot = ax * bx + ay * by + az * bz + aw * bw
    if dot < 0:
        bx, by, bz, bw = -bx, -by, -bz, -bw
    qx = ax + (bx - ax) * ratio
    qy = ay + (by - ay) * ratio
    qz = az + (bz - az) * ratio
    qw = aw + (bw - aw) * ratio
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm <= 1e-12:
        return before
    return qx / norm, qy / norm, qz / norm, qw / norm


def frame_path_for_timestamp(pairs: list[PairFrame], timestamp_ms: int) -> Path | None:
    if not pairs:
        return None
    nearest = min(pairs, key=lambda item: abs(item.image_timestamp_ms - timestamp_ms))
    return nearest.image_path


def _ns_to_ms(value: str) -> int:
    return int(round(int(value) / 1_000_000))


def _resolve_pose_timestamp_ms(row: dict[str, str]) -> int:
    raw = row.get("header_timestamp_ns") or row.get("bag_timestamp_ns") or ""
    if not raw:
        raise ValueError("Pose row is missing a timestamp field")
    return _ns_to_ms(raw)


def _median_diff(values: list[int]) -> int:
    if len(values) < 2:
        return 1000
    return int(statistics.median([b - a for a, b in zip(values, values[1:])]))
