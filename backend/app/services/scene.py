from __future__ import annotations

import collections
import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..settings import (
    SCENE_CALIBRATION_SAMPLE_FRAMES,
    SCENE_CALIBRATION_SAMPLE_POINTS,
    SCENE_EGO_FILTER_ENABLED,
    SCENE_EGO_FILTER_EXPAND_CELLS,
    SCENE_EGO_FILTER_MAX_X,
    SCENE_EGO_FILTER_MAX_Y,
    SCENE_EGO_FILTER_MAX_Z,
    SCENE_EGO_FILTER_MIN_HIT_RATIO,
    SCENE_EGO_FILTER_MIN_X,
    SCENE_EGO_FILTER_MIN_Y,
    SCENE_EGO_FILTER_MIN_Z,
    SCENE_EGO_FILTER_SAMPLE_FRAMES,
    SCENE_EGO_FILTER_VOXEL_SIZE,
    SCENE_EGO_SWEEP_FILTER_ENABLED,
    SCENE_EGO_SWEEP_MAX_Z_OFFSET,
    SCENE_EGO_SWEEP_MIN_Z_OFFSET,
    SCENE_EGO_SWEEP_RADIUS,
    SCENE_EGO_SWEEP_SAMPLE_COUNT,
    SCENE_FLOOR_CUT_MAX_LIFT,
    SCENE_FLOOR_CUT_MIN_LIFT,
    SCENE_FLOOR_CUT_QUANTILE,
    SCENE_HIGH_OUTLIER_QUANTILE,
    SCENE_LOW_OUTLIER_QUANTILE,
    SCENE_MAX_POINTS,
    SCENE_MAX_SOURCE_FRAMES,
    SCENE_RENDER_MAX_POINTS,
    SCENE_RENDER_TARGET_POINTS,
    SCENE_ROOF_QUANTILE,
    SCENE_VOXEL_SIZE,
)
from .calibration import CameraCalibration, load_camera_calibration
from .dataset import PairFrame, PoseRecord, PoseTimeline, sample_trajectory_with_orientations
from .storage import write_json


DEFAULT_POINT_MODE = "roof_removed"
_CAMERA_MATRIX_CACHE: dict[Path, np.ndarray] = {}


@dataclass(slots=True)
class CloudPoint:
    x: float
    y: float
    z: float
    intensity: float
    density: float
    rgb: tuple[float, float, float] | None = None


@dataclass(slots=True)
class Transform3D:
    rotation: np.ndarray
    translation: np.ndarray
    source: str


@dataclass(slots=True)
class ColorizationDecision:
    enabled: bool
    rotation: np.ndarray | None
    translation: np.ndarray | None
    mode: str
    positive_ratio: float
    in_frame_ratio: float
    note: str


@dataclass(slots=True)
class ProjectionAttempt:
    mode: str
    rotation: np.ndarray
    translation: np.ndarray
    score: float
    positive_ratio: float
    in_frame_ratio: float


@dataclass(slots=True)
class PoseValidityWindow:
    start_ms: int
    end_ms: int
    source: str


@dataclass(slots=True)
class EgoArtifactFilter:
    enabled: bool
    voxel_size: float
    min_hit_ratio: float
    min_hit_count: int
    sampled_frame_count: int
    candidate_point_count: int
    core_voxel_count: int
    expanded_voxel_count: int
    candidate_bounds: dict[str, list[float]]
    artifact_voxels: frozenset[tuple[int, int, int]]


def _resolve_pose_validity_window(
    poses: list[PoseRecord],
    valid_pose_reference: list[PoseRecord] | None,
    valid_pose_source: str,
) -> PoseValidityWindow:
    source_poses = valid_pose_reference or poses
    if not source_poses:
        raise ValueError("Cannot resolve a pose validity window without pose samples")
    timestamps = sorted(pose.timestamp_ms for pose in source_poses)
    return PoseValidityWindow(
        start_ms=timestamps[0],
        end_ms=timestamps[-1],
        source=valid_pose_source,
    )


def build_scene(
    pairs: list[PairFrame],
    poses: list[PoseRecord],
    output_path: Path,
    pose_topic: str,
    *,
    calibration_path: Path | None = None,
    tf_static_path: Path | None = None,
    valid_pose_reference: list[PoseRecord] | None = None,
    valid_pose_source: str | None = None,
) -> dict[str, object]:
    if not pairs:
        raise ValueError("Cannot build a scene without point-cloud pairs")
    if not poses:
        raise ValueError("Cannot build a scene without pose samples")

    sampled_pairs = _sample_pairs(pairs)
    pose_timeline = PoseTimeline(poses)
    pose_window = _resolve_pose_validity_window(poses, valid_pose_reference, valid_pose_source or pose_topic)
    lidar_mount = _load_lidar_mount_transform(tf_static_path)
    calibration_notes: list[str] = []
    colorization = ColorizationDecision(
        enabled=False,
        rotation=None,
        translation=None,
        mode="structure_only",
        positive_ratio=0.0,
        in_frame_ratio=0.0,
        note="camera color projection disabled for full LiDAR structure rendering",
    )
    ego_filter_pairs = [
        pair for pair in sampled_pairs if pose_window.start_ms <= pair.point_timestamp_ms <= pose_window.end_ms
    ]
    ego_filter = _build_ego_artifact_filter(ego_filter_pairs, lidar_mount)

    voxel_map: dict[tuple[int, int, int], list[float]] = {}
    used_pairs = 0
    input_point_count = 0
    ego_filtered_point_count = 0
    skipped_before_pose_window = 0
    skipped_after_pose_window = 0
    skipped_missing_pose = 0
    interpolated_pose_count = 0
    max_interpolation_gap_ms = 0
    consistency_residuals: list[float] = []
    consistency_sample_interval = max(1, len(sampled_pairs) // 120)

    for pair in sampled_pairs:
        if pair.point_timestamp_ms < pose_window.start_ms:
            skipped_before_pose_window += 1
            continue
        if pair.point_timestamp_ms > pose_window.end_ms:
            skipped_after_pose_window += 1
            continue
        pose_sample = pose_timeline.at(pair.point_timestamp_ms)
        if pose_sample is None:
            skipped_missing_pose += 1
            continue
        if pose_sample.interpolated:
            interpolated_pose_count += 1
        max_interpolation_gap_ms = max(max_interpolation_gap_ms, pose_sample.source_gap_ms)

        raw_points = load_point_rows(pair.point_path)
        if raw_points.size == 0:
            continue
        input_point_count += int(raw_points.shape[0])
        raw_points, removed_count = _filter_ego_artifacts(raw_points, ego_filter, lidar_mount)
        ego_filtered_point_count += removed_count
        if raw_points.size == 0:
            continue

        world_xyz = _transform_points_to_world(raw_points[:, :3], pose_sample.pose, lidar_mount)
        if used_pairs > 0 and used_pairs % consistency_sample_interval == 0:
            _append_frame_consistency_residuals(voxel_map, world_xyz, consistency_residuals)
        _accumulate_world_points(voxel_map, world_xyz, raw_points[:, 3], None, None)
        used_pairs += 1

    if not voxel_map:
        raise ValueError("No usable lidar points were accumulated into the scene")
    if used_pairs < 10:
        raise ValueError(f"Only {used_pairs} lidar frames had valid localization; refusing to build a misleading scene")

    trajectory_poses = [pose for pose in poses if pose_window.start_ms <= pose.timestamp_ms <= pose_window.end_ms]
    trajectory, trajectory_timestamps, trajectory_orientations = sample_trajectory_with_orientations(trajectory_poses or poses)
    finalized_candidates = _finalize_voxels(voxel_map)
    swept_candidates, ego_sweep_filtered_point_count = _filter_trajectory_sweep_artifacts(finalized_candidates, trajectory)
    full_candidates = _trim_vertical_outliers(swept_candidates)
    if not full_candidates:
        raise ValueError("The accumulated point cloud became empty after outlier trimming")

    roof_threshold, roof_context = _roof_threshold(full_candidates)
    roof_candidates = [point for point in full_candidates if point.z <= roof_threshold]
    roof_source = roof_candidates or full_candidates
    floor_threshold, floor_context = _floor_threshold(roof_source)
    floor_candidates = [point for point in roof_source if point.z >= floor_threshold]
    active_source = floor_candidates if len(floor_candidates) >= max(2000, int(len(roof_source) * 0.2)) else roof_source
    floor_cut_applied = active_source is floor_candidates
    default_point_mode = DEFAULT_POINT_MODE if roof_candidates else "full"

    full_points = _serialize_points(_select_points(full_candidates, trajectory, max_points=SCENE_MAX_POINTS))
    roof_removed_points = _serialize_points(_select_points(roof_source, trajectory, max_points=SCENE_MAX_POINTS))
    floor_removed_points = _serialize_points(_select_points(active_source, trajectory, max_points=SCENE_MAX_POINTS))
    structure_cloud, render_voxel_size = _build_structure_points(roof_source, trajectory)
    structure_points = _serialize_points(structure_cloud)
    render_points = structure_points
    active_points = roof_removed_points if roof_removed_points else full_points

    colorized = False
    color_source = "lidar_intensity_structure"
    input_frame_count = len(pairs)
    frame_limit = SCENE_MAX_SOURCE_FRAMES if SCENE_MAX_SOURCE_FRAMES > 0 else None
    skipped_frame_count = skipped_before_pose_window + skipped_after_pose_window + skipped_missing_pose
    ego_total_filtered_point_count = ego_filtered_point_count + ego_sweep_filtered_point_count
    roof_filtered_ratio = 1.0 - (len(roof_source) / max(1, len(full_candidates)))
    floor_filtered_ratio = 1.0 - (len(active_source) / max(1, len(roof_source)))
    roof_cut_height = round(float(min(roof_threshold, _max_z(active_source))), 4)
    floor_cut_height = round(float(floor_threshold), 4)
    consistency_stats = _summarize_frame_consistency_residuals(consistency_residuals)

    payload = {
        "points": active_points,
        "full_points": full_points,
        "roof_removed_points": roof_removed_points,
        "floor_removed_points": floor_removed_points,
        "structure_points": structure_points,
        "render_points": render_points,
        "default_point_mode": default_point_mode,
        "trajectory": trajectory,
        "trajectory_timestamps": trajectory_timestamps,
        "trajectory_orientations": trajectory_orientations,
        "bounds": _compute_bounds(roof_source, trajectory),
        "full_bounds": _compute_bounds(full_candidates, trajectory),
        "roof_removed_bounds": _compute_bounds(roof_source, trajectory),
        "source_frame_count": used_pairs,
        "coordinate_frame": "global",
        "source_type": "pcd_accumulated_lidar_structure",
        "raw_point_count": len(roof_source),
        "render_point_count": len(render_points),
        "structure_point_count": len(structure_points),
        "colorized": colorized,
        "color_source": color_source,
        "cut_height_default": roof_cut_height,
        "floor_cut_default": floor_cut_height,
        "scene_quality": {
            "base_voxel_size_m": SCENE_VOXEL_SIZE,
            "structure_voxel_size_m": round(render_voxel_size, 4),
            "roof_cut_height_m": roof_cut_height,
            "floor_cut_height_m": floor_cut_height,
            "floor_cut_quantile": SCENE_FLOOR_CUT_QUANTILE,
            "floor_cut_min_lift_m": SCENE_FLOOR_CUT_MIN_LIFT,
            "floor_cut_max_lift_m": SCENE_FLOOR_CUT_MAX_LIFT,
            "input_frame_count": input_frame_count,
            "input_point_count": input_point_count,
            "used_frame_count": used_pairs,
            "skipped_frame_count": skipped_frame_count,
            "skipped_before_pose_window": skipped_before_pose_window,
            "skipped_after_pose_window": skipped_after_pose_window,
            "skipped_missing_pose": skipped_missing_pose,
            "source_frame_limit": frame_limit,
            "pose_source": pose_topic,
            "pose_validity_source": pose_window.source,
            "pose_valid_start_ms": pose_window.start_ms,
            "pose_valid_end_ms": pose_window.end_ms,
            "pose_interpolation_enabled": True,
            "interpolated_pose_count": interpolated_pose_count,
            "max_pose_interpolation_gap_ms": max_interpolation_gap_ms,
            "pose_timeline_max_gap_ms": pose_timeline.max_gap_ms,
            "ego_artifact_filter_enabled": ego_filter.enabled,
            "ego_artifact_filtered_point_count": ego_filtered_point_count,
            "ego_artifact_bbox_filtered_point_count": ego_filtered_point_count,
            "ego_artifact_bbox_filtered_ratio": round(ego_filtered_point_count / max(1, input_point_count), 6),
            "ego_trajectory_sweep_filter_enabled": SCENE_EGO_SWEEP_FILTER_ENABLED,
            "ego_trajectory_sweep_filtered_point_count": ego_sweep_filtered_point_count,
            "ego_total_filtered_point_count": ego_total_filtered_point_count,
            "ego_total_filtered_ratio": round(ego_total_filtered_point_count / max(1, input_point_count), 6),
            "ego_trajectory_sweep_radius_m": SCENE_EGO_SWEEP_RADIUS,
            "ego_trajectory_sweep_min_z_offset_m": SCENE_EGO_SWEEP_MIN_Z_OFFSET,
            "ego_trajectory_sweep_max_z_offset_m": SCENE_EGO_SWEEP_MAX_Z_OFFSET,
            "ego_artifact_filter_voxel_size_m": round(ego_filter.voxel_size, 4),
            "ego_artifact_filter_min_hit_ratio": round(ego_filter.min_hit_ratio, 4),
            "ego_artifact_filter_min_hit_count": ego_filter.min_hit_count,
            "ego_artifact_filter_sampled_frame_count": ego_filter.sampled_frame_count,
            "ego_artifact_filter_candidate_point_count": ego_filter.candidate_point_count,
            "ego_artifact_filter_core_voxel_count": ego_filter.core_voxel_count,
            "ego_artifact_filter_expanded_voxel_count": ego_filter.expanded_voxel_count,
            "ego_artifact_filter_bounds": ego_filter.candidate_bounds,
            "pre_sweep_candidate_count": len(finalized_candidates),
            "full_candidate_count": len(full_candidates),
            "roof_removed_count": len(roof_source),
            "floor_removed_count": len(active_source),
            "structure_point_count": len(structure_points),
            "render_point_count": len(render_points),
            "serialized_full_point_count": len(full_points),
            "serialized_roof_removed_point_count": len(roof_removed_points),
            "serialized_floor_removed_point_count": len(floor_removed_points),
            "serialized_structure_point_count": len(structure_points),
            "frontend_default_source": "roof_removed" if roof_removed_points else "full",
            "frontend_default_point_count": len(active_points),
            "structure_source": "roof_removed_points",
            "structure_source_count": len(roof_source),
            "structure_sampling_ratio": round(len(structure_points) / max(1, len(roof_source)), 6),
            "structure_intensity_source": "lidar_intensity",
            "render_target_points": SCENE_RENDER_TARGET_POINTS,
            "render_max_points": SCENE_RENDER_MAX_POINTS,
            "roof_removed_ratio": round(roof_filtered_ratio, 4),
            "floor_removed_ratio": round(floor_filtered_ratio, 4),
            **consistency_stats,
        },
        "notes": [
            f"Accumulated {used_pairs} of {input_frame_count} lidar frames into a global roof-off and floor-off structure scene.",
            f"Skipped {skipped_frame_count} lidar frames outside or missing the valid localization window.",
            f"Trajectory source: {pose_topic}; validity window source: {pose_window.source}.",
            f"Pose interpolation enabled; interpolated {interpolated_pose_count} frames with max source gap {max_interpolation_gap_ms} ms.",
            f"Ego bounding-box filter removed {ego_filtered_point_count} base_link near-vehicle points.",
            f"Ego trajectory sweep filter removed {ego_sweep_filtered_point_count} low residual points near the robot path.",
            f"Base voxel size: {SCENE_VOXEL_SIZE} m; structure render voxel size: {render_voxel_size:.3f} m.",
            f"Roof-off cut height: z={roof_threshold:.2f} m ({roof_context}).",
            f"Floor-off cut height: z={floor_threshold:.2f} m ({floor_context}; applied={floor_cut_applied}; not used as the default front-end crop).",
            f"Structure layer contains {len(structure_points)} display points selected from {len(roof_source)} roof-off points while preserving LiDAR intensity.",
            f"Serialized default front-end source contains {len(active_points)} roof-off points.",
            f"Color source: {color_source}; {colorization.note}.",
            *calibration_notes,
        ],
    }
    write_json(output_path, payload)
    return payload


def load_point_rows(path: Path) -> np.ndarray:
    suffix = path.suffix.lower()
    if suffix == ".pcd":
        return _load_pcd_rows(path)
    rows = np.loadtxt(path, usecols=(0, 1, 2, 3), dtype=np.float32)
    if rows.ndim == 1:
        rows = rows.reshape(1, 4)
    return rows


def _sample_pairs(pairs: list[PairFrame]) -> list[PairFrame]:
    if SCENE_MAX_SOURCE_FRAMES <= 0 or len(pairs) <= SCENE_MAX_SOURCE_FRAMES:
        return pairs
    if SCENE_MAX_SOURCE_FRAMES == 1:
        return [pairs[len(pairs) // 2]]
    indices = sorted(
        {round(index * (len(pairs) - 1) / (SCENE_MAX_SOURCE_FRAMES - 1)) for index in range(SCENE_MAX_SOURCE_FRAMES)}
    )
    return [pairs[index] for index in indices]


def _load_lidar_mount_transform(tf_static_path: Path | None) -> Transform3D:
    identity = Transform3D(rotation=np.eye(3, dtype=np.float64), translation=np.zeros(3, dtype=np.float64), source="identity")
    if tf_static_path is None or not tf_static_path.exists():
        return identity

    with tf_static_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("parent_frame") != "base_link" or row.get("child_frame") != "rslidar":
                continue
            rotation = _rotation_matrix_from_quaternion(
                float(row["qx"]),
                float(row["qy"]),
                float(row["qz"]),
                float(row["qw"]),
            )
            translation = np.asarray([float(row["tx"]), float(row["ty"]), float(row["tz"])], dtype=np.float64)
            return Transform3D(rotation=rotation, translation=translation, source="tf_static")
    return identity


def _build_ego_artifact_filter(pairs: list[PairFrame], lidar_mount: Transform3D) -> EgoArtifactFilter:
    lower, upper, bounds = _ego_filter_bounds()
    voxel_size = max(0.03, float(SCENE_EGO_FILTER_VOXEL_SIZE))
    if not SCENE_EGO_FILTER_ENABLED:
        return _empty_ego_artifact_filter(voxel_size, bounds)
    if not pairs or SCENE_EGO_FILTER_SAMPLE_FRAMES <= 0:
        return _empty_ego_artifact_filter(voxel_size, bounds, enabled=True)

    sample_count = min(len(pairs), max(1, SCENE_EGO_FILTER_SAMPLE_FRAMES))
    sample_indices = sorted(
        {round(index * (len(pairs) - 1) / max(1, sample_count - 1)) for index in range(sample_count)}
    )
    hit_counts: collections.Counter[tuple[int, int, int]] = collections.Counter()
    sampled_frame_count = 0
    candidate_point_count = 0

    for index in sample_indices:
        raw_points = load_point_rows(pairs[index].point_path)
        if raw_points.size == 0:
            continue

        sampled_frame_count += 1
        base_xyz = _transform_lidar_points_to_base(raw_points[:, :3], lidar_mount)
        candidate_xyz = base_xyz[_ego_candidate_mask(base_xyz, lower, upper)]
        if candidate_xyz.size == 0:
            continue

        candidate_point_count += int(candidate_xyz.shape[0])
        keys = np.floor(candidate_xyz / voxel_size).astype(np.int32)
        unique_keys = np.unique(keys, axis=0)
        hit_counts.update((int(row[0]), int(row[1]), int(row[2])) for row in unique_keys)

    if sampled_frame_count == 0 or not hit_counts:
        return _empty_ego_artifact_filter(
            voxel_size,
            bounds,
            sampled_frame_count,
            candidate_point_count,
            enabled=True,
        )

    min_hit_count = max(3, math.ceil(sampled_frame_count * SCENE_EGO_FILTER_MIN_HIT_RATIO))
    core_voxels = {key for key, count in hit_counts.items() if count >= min_hit_count}
    artifact_voxels = _expand_voxel_keys(core_voxels, max(0, SCENE_EGO_FILTER_EXPAND_CELLS))

    return EgoArtifactFilter(
        enabled=True,
        voxel_size=voxel_size,
        min_hit_ratio=SCENE_EGO_FILTER_MIN_HIT_RATIO,
        min_hit_count=min_hit_count,
        sampled_frame_count=sampled_frame_count,
        candidate_point_count=candidate_point_count,
        core_voxel_count=len(core_voxels),
        expanded_voxel_count=len(artifact_voxels),
        candidate_bounds=bounds,
        artifact_voxels=frozenset(artifact_voxels),
    )


def _empty_ego_artifact_filter(
    voxel_size: float,
    bounds: dict[str, list[float]],
    sampled_frame_count: int = 0,
    candidate_point_count: int = 0,
    *,
    enabled: bool = False,
) -> EgoArtifactFilter:
    return EgoArtifactFilter(
        enabled=enabled,
        voxel_size=voxel_size,
        min_hit_ratio=SCENE_EGO_FILTER_MIN_HIT_RATIO,
        min_hit_count=0,
        sampled_frame_count=sampled_frame_count,
        candidate_point_count=candidate_point_count,
        core_voxel_count=0,
        expanded_voxel_count=0,
        candidate_bounds=bounds,
        artifact_voxels=frozenset(),
    )


def _filter_ego_artifacts(
    raw_points: np.ndarray,
    ego_filter: EgoArtifactFilter,
    lidar_mount: Transform3D,
) -> tuple[np.ndarray, int]:
    if not ego_filter.enabled or raw_points.size == 0:
        return raw_points, 0

    lower, upper, _ = _ego_filter_bounds()
    base_xyz = _transform_lidar_points_to_base(raw_points[:, :3], lidar_mount)
    remove_mask = _ego_candidate_mask(base_xyz, lower, upper)

    if ego_filter.artifact_voxels:
        candidate_indices = np.flatnonzero(remove_mask)
        if candidate_indices.size > 0:
            candidate_keys = np.floor(base_xyz[candidate_indices] / ego_filter.voxel_size).astype(np.int32)
            remove_local = np.fromiter(
                ((int(row[0]), int(row[1]), int(row[2])) in ego_filter.artifact_voxels for row in candidate_keys),
                dtype=bool,
                count=int(candidate_keys.shape[0]),
            )
            remove_mask[candidate_indices[remove_local]] = True

    if not np.any(remove_mask):
        return raw_points, 0

    return raw_points[~remove_mask], int(np.count_nonzero(remove_mask))


def _ego_filter_bounds() -> tuple[np.ndarray, np.ndarray, dict[str, list[float]]]:
    lower = np.asarray(
        [SCENE_EGO_FILTER_MIN_X, SCENE_EGO_FILTER_MIN_Y, SCENE_EGO_FILTER_MIN_Z],
        dtype=np.float64,
    )
    upper = np.asarray(
        [SCENE_EGO_FILTER_MAX_X, SCENE_EGO_FILTER_MAX_Y, SCENE_EGO_FILTER_MAX_Z],
        dtype=np.float64,
    )
    bounds = {
        "min": [round(float(value), 4) for value in lower],
        "max": [round(float(value), 4) for value in upper],
    }
    return lower, upper, bounds


def _ego_candidate_mask(base_xyz: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> np.ndarray:
    finite = np.isfinite(base_xyz).all(axis=1)
    return finite & np.all(base_xyz >= lower, axis=1) & np.all(base_xyz <= upper, axis=1)


def _expand_voxel_keys(keys: set[tuple[int, int, int]], radius: int) -> set[tuple[int, int, int]]:
    if radius <= 0:
        return set(keys)
    expanded: set[tuple[int, int, int]] = set()
    for key_x, key_y, key_z in keys:
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    expanded.add((key_x + dx, key_y + dy, key_z + dz))
    return expanded


def _transform_lidar_points_to_base(points_xyz: np.ndarray, lidar_mount: Transform3D) -> np.ndarray:
    return points_xyz @ lidar_mount.rotation.T + lidar_mount.translation


def _filter_trajectory_sweep_artifacts(
    points: list[CloudPoint],
    trajectory: list[list[float]],
) -> tuple[list[CloudPoint], int]:
    if not SCENE_EGO_SWEEP_FILTER_ENABLED or not points or not trajectory:
        return points, 0

    radius = max(0.05, SCENE_EGO_SWEEP_RADIUS)
    lookup, cell_size = _build_trajectory_sweep_lookup(trajectory, radius)
    if not lookup:
        return points, 0

    radius_sq = radius * radius
    kept: list[CloudPoint] = []
    removed_count = 0
    for point in points:
        nearest = _nearest_trajectory_xy(point, lookup, cell_size)
        if nearest is None:
            kept.append(point)
            continue

        distance_sq, trajectory_z = nearest
        z_offset = point.z - trajectory_z
        near_core = distance_sq <= radius_sq
        low_vehicle_band = SCENE_EGO_SWEEP_MIN_Z_OFFSET <= z_offset <= SCENE_EGO_SWEEP_MAX_Z_OFFSET
        sparse_residual = point.density <= 2.0
        very_low = z_offset <= 0.45
        if (
            near_core
            and low_vehicle_band
            and (sparse_residual or very_low)
        ):
            removed_count += 1
            continue
        kept.append(point)

    return kept, removed_count


def _build_trajectory_sweep_lookup(
    trajectory: list[list[float]],
    cell_size: float,
) -> tuple[dict[tuple[int, int], list[tuple[float, float, float]]], float]:
    samples = _sample_vectors(trajectory, max(1, SCENE_EGO_SWEEP_SAMPLE_COUNT))
    lookup: dict[tuple[int, int], list[tuple[float, float, float]]] = collections.defaultdict(list)
    for x, y, z in samples:
        lookup[(int(math.floor(x / cell_size)), int(math.floor(y / cell_size)))].append((x, y, z))
    return lookup, cell_size


def _nearest_trajectory_xy(
    point: CloudPoint,
    lookup: dict[tuple[int, int], list[tuple[float, float, float]]],
    cell_size: float,
) -> tuple[float, float] | None:
    key_x = int(math.floor(point.x / cell_size))
    key_y = int(math.floor(point.y / cell_size))
    best: tuple[float, float] | None = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for tx, ty, tz in lookup.get((key_x + dx, key_y + dy), []):
                distance_sq = (point.x - tx) ** 2 + (point.y - ty) ** 2
                if best is None or distance_sq < best[0]:
                    best = (distance_sq, tz)
    return best


def _resolve_colorization(
    sampled_pairs: list[PairFrame],
    calibration_path: Path | None,
    notes: list[str],
) -> ColorizationDecision:
    if calibration_path is None or not calibration_path.exists():
        return ColorizationDecision(
            enabled=False,
            rotation=None,
            translation=None,
            mode="disabled",
            positive_ratio=0.0,
            in_frame_ratio=0.0,
            note="security_check.yaml not found",
        )

    try:
        calibration = load_camera_calibration(calibration_path)
    except Exception as exc:
        return ColorizationDecision(
            enabled=False,
            rotation=None,
            translation=None,
            mode="disabled",
            positive_ratio=0.0,
            in_frame_ratio=0.0,
            note=f"failed to parse calibration: {exc}",
        )

    _CAMERA_MATRIX_CACHE[calibration_path] = calibration.camera_matrix

    direct_rotation = calibration.rotation_matrix
    direct_translation = calibration.translation_vector
    inverse_rotation = calibration.rotation_matrix.T
    inverse_translation = -inverse_rotation @ calibration.translation_vector

    attempts = [
        _score_projection_attempt(sampled_pairs, calibration, direct_rotation, direct_translation, "direct"),
        _score_projection_attempt(sampled_pairs, calibration, inverse_rotation, inverse_translation, "inverse"),
    ]
    attempts.sort(key=lambda item: item.score, reverse=True)
    best = attempts[0]
    notes.append(
        "Calibration candidates: "
        + ", ".join(
            f"{attempt.mode}(score={attempt.score:.3f}, positive={attempt.positive_ratio:.3f}, in-frame={attempt.in_frame_ratio:.3f})"
            for attempt in attempts
        )
    )

    if best.in_frame_ratio < 0.01 or best.positive_ratio < 0.08:
        return ColorizationDecision(
            enabled=False,
            rotation=None,
            translation=None,
            mode="disabled",
            positive_ratio=best.positive_ratio,
            in_frame_ratio=best.in_frame_ratio,
            note="projection success ratio too low; keeping structure-enhanced fallback",
        )

    return ColorizationDecision(
        enabled=True,
        rotation=best.rotation,
        translation=best.translation,
        mode=best.mode,
        positive_ratio=best.positive_ratio,
        in_frame_ratio=best.in_frame_ratio,
        note="",
    )


def _project_point_colors(
    image_path: Path,
    lidar_xyz: np.ndarray,
    colorization: ColorizationDecision,
    calibration_path: Path | None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    if (
        not colorization.enabled
        or colorization.rotation is None
        or colorization.translation is None
        or calibration_path is None
    ):
        return None, None

    try:
        image = np.asarray(Image.open(image_path).convert("RGB"), dtype=np.float32)
    except Exception:
        return None, None

    camera_matrix = _camera_matrix(calibration_path)
    projected = lidar_xyz @ colorization.rotation.T + colorization.translation
    depth = projected[:, 2]
    positive_mask = depth > 0.1
    if not np.any(positive_mask):
        return None, None

    fx = float(camera_matrix[0, 0])
    fy = float(camera_matrix[1, 1])
    cx = float(camera_matrix[0, 2])
    cy = float(camera_matrix[1, 2])

    u = fx * (projected[:, 0] / np.maximum(depth, 1e-6)) + cx
    v = fy * (projected[:, 1] / np.maximum(depth, 1e-6)) + cy
    height, width = image.shape[:2]
    valid = positive_mask & (u >= 0) & (u < width) & (v >= 0) & (v < height)
    if not np.any(valid):
        return None, None

    ui = np.clip(np.rint(u[valid]).astype(np.int32), 0, width - 1)
    vi = np.clip(np.rint(v[valid]).astype(np.int32), 0, height - 1)
    colors = np.zeros((lidar_xyz.shape[0], 3), dtype=np.float32)
    colors[valid] = image[vi, ui, :]
    return colors, valid


def _transform_points_to_world(points_xyz: np.ndarray, pose: PoseRecord, lidar_mount: Transform3D) -> np.ndarray:
    pose_rotation = _rotation_matrix_from_quaternion(pose.qx, pose.qy, pose.qz, pose.qw)
    pose_translation = np.asarray([pose.x, pose.y, pose.z], dtype=np.float64)
    base_xyz = _transform_lidar_points_to_base(points_xyz, lidar_mount)
    return base_xyz @ pose_rotation.T + pose_translation


def _accumulate_world_points(
    voxel_map: dict[tuple[int, int, int], list[float]],
    world_xyz: np.ndarray,
    intensity: np.ndarray,
    colors: np.ndarray | None,
    color_mask: np.ndarray | None,
) -> None:
    keys = np.floor(world_xyz / SCENE_VOXEL_SIZE).astype(np.int32)
    unique_keys, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    bucket_count = int(unique_keys.shape[0])

    sum_x = np.bincount(inverse, weights=world_xyz[:, 0], minlength=bucket_count)
    sum_y = np.bincount(inverse, weights=world_xyz[:, 1], minlength=bucket_count)
    sum_z = np.bincount(inverse, weights=world_xyz[:, 2], minlength=bucket_count)
    sum_i = np.bincount(inverse, weights=intensity, minlength=bucket_count)

    if colors is not None and color_mask is not None:
        mask = color_mask.astype(np.float64)
        sum_r = np.bincount(inverse, weights=colors[:, 0] * mask, minlength=bucket_count)
        sum_g = np.bincount(inverse, weights=colors[:, 1] * mask, minlength=bucket_count)
        sum_b = np.bincount(inverse, weights=colors[:, 2] * mask, minlength=bucket_count)
        color_hits = np.bincount(inverse, weights=mask, minlength=bucket_count)
    else:
        sum_r = np.zeros(bucket_count, dtype=np.float64)
        sum_g = np.zeros(bucket_count, dtype=np.float64)
        sum_b = np.zeros(bucket_count, dtype=np.float64)
        color_hits = np.zeros(bucket_count, dtype=np.float64)

    for index, key_row in enumerate(unique_keys):
        key = (int(key_row[0]), int(key_row[1]), int(key_row[2]))
        bucket = voxel_map.get(key)
        if bucket is None:
            bucket = [0.0] * 9
            voxel_map[key] = bucket
        bucket[0] += float(sum_x[index])
        bucket[1] += float(sum_y[index])
        bucket[2] += float(sum_z[index])
        bucket[3] += float(sum_i[index])
        bucket[4] += float(counts[index])
        bucket[5] += float(sum_r[index])
        bucket[6] += float(sum_g[index])
        bucket[7] += float(sum_b[index])
        bucket[8] += float(color_hits[index])


def _append_frame_consistency_residuals(
    voxel_map: dict[tuple[int, int, int], list[float]],
    world_xyz: np.ndarray,
    residuals: list[float],
    *,
    max_total_samples: int = 50000,
    max_frame_samples: int = 160,
) -> None:
    if not voxel_map or world_xyz.size == 0 or len(residuals) >= max_total_samples:
        return

    sample_count = min(max_frame_samples, max_total_samples - len(residuals), int(world_xyz.shape[0]))
    if sample_count <= 0:
        return

    step = max(1, int(world_xyz.shape[0]) // sample_count)
    search_radius = 2
    for row in world_xyz[::step][:sample_count]:
        base_key = (
            int(math.floor(float(row[0]) / SCENE_VOXEL_SIZE)),
            int(math.floor(float(row[1]) / SCENE_VOXEL_SIZE)),
            int(math.floor(float(row[2]) / SCENE_VOXEL_SIZE)),
        )
        best_distance_sq: float | None = None
        for dx in range(-search_radius, search_radius + 1):
            for dy in range(-search_radius, search_radius + 1):
                for dz in range(-search_radius, search_radius + 1):
                    bucket = voxel_map.get((base_key[0] + dx, base_key[1] + dy, base_key[2] + dz))
                    if bucket is None or bucket[4] <= 0:
                        continue
                    weight = bucket[4]
                    distance_sq = (
                        (float(row[0]) - bucket[0] / weight) ** 2
                        + (float(row[1]) - bucket[1] / weight) ** 2
                        + (float(row[2]) - bucket[2] / weight) ** 2
                    )
                    if best_distance_sq is None or distance_sq < best_distance_sq:
                        best_distance_sq = distance_sq
        if best_distance_sq is not None:
            residuals.append(math.sqrt(best_distance_sq))


def _summarize_frame_consistency_residuals(residuals: list[float]) -> dict[str, float | int | None]:
    if not residuals:
        return {
            "frame_consistency_sample_count": 0,
            "frame_consistency_residual_median_m": None,
            "frame_consistency_residual_p90_m": None,
        }

    values = sorted(residuals)
    return {
        "frame_consistency_sample_count": len(values),
        "frame_consistency_residual_median_m": round(_percentile(values, 0.5), 4),
        "frame_consistency_residual_p90_m": round(_percentile(values, 0.9), 4),
    }


def _finalize_voxels(voxel_map: dict[tuple[int, int, int], list[float]]) -> list[CloudPoint]:
    points: list[CloudPoint] = []
    for sums in voxel_map.values():
        count = max(1.0, sums[4])
        rgb = None
        if sums[8] > 0:
            rgb = (sums[5] / sums[8], sums[6] / sums[8], sums[7] / sums[8])
        points.append(
            CloudPoint(
                x=sums[0] / count,
                y=sums[1] / count,
                z=sums[2] / count,
                intensity=sums[3] / count,
                density=count,
                rgb=rgb,
            )
        )
    return points


def _trim_vertical_outliers(points: list[CloudPoint]) -> list[CloudPoint]:
    if not points:
        return []
    z_values = sorted(point.z for point in points)
    lower = _percentile(z_values, SCENE_LOW_OUTLIER_QUANTILE)
    upper = _percentile(z_values, SCENE_HIGH_OUTLIER_QUANTILE)
    return [point for point in points if lower <= point.z <= upper]


def _roof_threshold(points: list[CloudPoint]) -> tuple[float, str]:
    if not points:
        return 0.0, "empty cloud"
    z_values = sorted(point.z for point in points)
    percentile_threshold = _percentile(z_values, SCENE_ROOF_QUANTILE)
    layer_threshold, layer_context = _roof_layer_threshold(points, z_values)
    if layer_threshold is None:
        return percentile_threshold, f"percentile fallback q={SCENE_ROOF_QUANTILE:.2f}"
    return min(percentile_threshold, layer_threshold), layer_context


def _floor_threshold(points: list[CloudPoint]) -> tuple[float, str]:
    if not points:
        return 0.0, "empty cloud"
    z_values = sorted(point.z for point in points)
    low = _percentile(z_values, 0.02)
    floor_layer = _percentile(z_values, 0.10)
    structural_floor = _percentile(z_values, SCENE_FLOOR_CUT_QUANTILE)
    mid_guard = _percentile(z_values, min(0.48, max(SCENE_FLOOR_CUT_QUANTILE + 0.08, 0.40)))
    lift = max(SCENE_FLOOR_CUT_MIN_LIFT, min(SCENE_FLOOR_CUT_MAX_LIFT, (mid_guard - low) * 0.48))
    threshold = min(mid_guard, max(structural_floor, floor_layer + lift))
    return (
        threshold,
        f"low={low:.2f} m, q10={floor_layer:.2f} m, "
        f"q{SCENE_FLOOR_CUT_QUANTILE:.2f}={structural_floor:.2f} m, "
        f"guard={mid_guard:.2f} m, lift={lift:.2f} m",
    )


def _select_points(points: list[CloudPoint], trajectory: list[list[float]], max_points: int) -> list[CloudPoint]:
    if len(points) <= max_points:
        return points

    min_z = min(point.z for point in points)
    max_z = max(point.z for point in points)
    min_intensity = min(point.intensity for point in points)
    max_intensity = max(point.intensity for point in points)
    max_density = max(point.density for point in points)
    z_range = max(0.001, max_z - min_z)
    intensity_range = max(0.001, max_intensity - min_intensity)
    trajectory_samples = _sample_vectors(trajectory, 96)

    scored: list[tuple[float, CloudPoint]] = []
    for point in points:
        trajectory_distance = _nearest_trajectory_distance_sq(point, trajectory_samples)
        height_score = (point.z - min_z) / z_range
        intensity_score = (point.intensity - min_intensity) / intensity_range
        density_score = math.log1p(point.density) / math.log1p(max_density)
        trajectory_score = 1.0 / (1.0 + trajectory_distance) if trajectory_samples else 0.0
        ego_trail_penalty = trajectory_score * (0.55 * (1.0 - _clamp(height_score / 0.45, 0.0, 1.0)) + 0.12)
        color_score = 0.22 if point.rgb is not None else 0.0
        score = density_score * 1.15 + height_score * 0.45 + intensity_score * 0.18 + color_score - ego_trail_penalty
        scored.append((score, point))

    scored.sort(key=lambda item: item[0], reverse=True)
    focused_count = min(max_points, int(max_points * 0.72))
    focused = [point for _, point in scored[:focused_count]]
    remaining = [point for _, point in scored[focused_count:]]
    if not remaining or len(focused) >= max_points:
        return focused[:max_points]

    spread_needed = max_points - len(focused)
    step = max(1, len(remaining) // max(1, spread_needed))
    spread = remaining[::step][:spread_needed]
    return focused + spread


def _build_structure_points(points: list[CloudPoint], trajectory: list[list[float]]) -> tuple[list[CloudPoint], float]:
    if not points:
        return [], SCENE_VOXEL_SIZE

    voxel_size = _resolve_render_voxel_size(len(points))
    candidates = _aggregate_display_points(points, voxel_size)
    while len(candidates) > int(SCENE_RENDER_MAX_POINTS * 1.22) and voxel_size < SCENE_VOXEL_SIZE * 4.5:
        voxel_size *= 1.08
        candidates = _aggregate_display_points(points, voxel_size)

    structure_points = _select_structure_points(candidates, trajectory, SCENE_RENDER_MAX_POINTS, voxel_size)
    return structure_points, voxel_size


def _select_structure_points(
    points: list[CloudPoint],
    trajectory: list[list[float]],
    max_points: int,
    voxel_size: float,
) -> list[CloudPoint]:
    if not points:
        return []

    scored = _score_structure_points(points, trajectory, voxel_size)
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) <= max_points:
        selected = scored
    else:
        focused_count = min(max_points, int(max_points * 0.78))
        focused = scored[:focused_count]
        remaining = scored[focused_count:]
        spread_needed = max_points - len(focused)
        step = max(1, len(remaining) // max(1, spread_needed))
        selected = focused + remaining[::step][:spread_needed]

    if not selected:
        return []

    return [point for _, point in selected]


def _score_structure_points(
    points: list[CloudPoint],
    trajectory: list[list[float]],
    voxel_size: float,
) -> list[tuple[float, CloudPoint]]:
    min_z = min(point.z for point in points)
    max_z = max(point.z for point in points)
    min_intensity = min(point.intensity for point in points)
    max_intensity = max(point.intensity for point in points)
    max_density = max(point.density for point in points)
    z_range = max(0.001, max_z - min_z)
    intensity_range = max(0.001, max_intensity - min_intensity)
    density_range = max(0.001, math.log1p(max_density))
    cell_size = max(voxel_size, SCENE_VOXEL_SIZE)
    trajectory_lookup = _build_trajectory_lookup(trajectory, max(1.4, cell_size * 3.0))

    keys: dict[tuple[int, int, int], CloudPoint] = {}
    xy_stacks: collections.Counter[tuple[int, int]] = collections.Counter()
    for point in points:
        key = _structure_key(point, cell_size)
        keys[key] = point
        xy_stacks[(key[0], key[1])] += 1

    scored: list[tuple[float, CloudPoint]] = []
    for point in points:
        key = _structure_key(point, cell_size)
        height_score = (point.z - min_z) / z_range
        density_score = math.log1p(point.density) / density_range
        intensity_score = (point.intensity - min_intensity) / intensity_range
        horizontal_neighbors = _horizontal_neighbor_count(keys, key)
        edge_score = 1.0 - (horizontal_neighbors / 8.0)
        stack_score = _clamp((xy_stacks[(key[0], key[1])] - 1) / 5.0, 0.0, 1.0)
        trajectory_score = _trajectory_grid_score(point, trajectory_lookup)
        ego_trail_penalty = trajectory_score * (0.72 * (1.0 - _clamp(height_score / 0.45, 0.0, 1.0)) + 0.12)
        lower_band_penalty = (1.0 - _clamp(height_score / 0.18, 0.0, 1.0)) * 0.82
        upper_penalty = max(0.0, height_score - 0.78) * 0.7

        score = (
            density_score * 1.12
            + stack_score * 1.06
            + edge_score * 0.82
            + intensity_score * 0.22
            + height_score * 0.18
            - ego_trail_penalty
            - lower_band_penalty
            - upper_penalty
        )
        scored.append((score, point))
    return scored


def _structure_key(point: CloudPoint, cell_size: float) -> tuple[int, int, int]:
    return (
        int(math.floor(point.x / cell_size)),
        int(math.floor(point.y / cell_size)),
        int(math.floor(point.z / cell_size)),
    )


def _horizontal_neighbor_count(
    keys: dict[tuple[int, int, int], CloudPoint],
    key: tuple[int, int, int],
) -> int:
    count = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            if (key[0] + dx, key[1] + dy, key[2]) in keys:
                count += 1
    return count


def _build_trajectory_lookup(
    trajectory: list[list[float]],
    cell_size: float,
) -> tuple[dict[tuple[int, int], list[tuple[float, float]]], float]:
    samples = _sample_vectors(trajectory, 160)
    lookup: dict[tuple[int, int], list[tuple[float, float]]] = collections.defaultdict(list)
    for x, y, _ in samples:
        lookup[(int(math.floor(x / cell_size)), int(math.floor(y / cell_size)))].append((x, y))
    return lookup, cell_size


def _trajectory_grid_score(
    point: CloudPoint,
    lookup_context: tuple[dict[tuple[int, int], list[tuple[float, float]]], float],
) -> float:
    lookup, cell_size = lookup_context
    if not lookup:
        return 0.0
    key_x = int(math.floor(point.x / cell_size))
    key_y = int(math.floor(point.y / cell_size))
    best_distance_sq: float | None = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for tx, ty in lookup.get((key_x + dx, key_y + dy), []):
                distance_sq = (point.x - tx) ** 2 + (point.y - ty) ** 2
                if best_distance_sq is None or distance_sq < best_distance_sq:
                    best_distance_sq = distance_sq
    if best_distance_sq is None:
        return 0.0
    return 1.0 / (1.0 + best_distance_sq)


def _build_render_points(points: list[CloudPoint], trajectory: list[list[float]]) -> tuple[list[CloudPoint], float]:
    if not points:
        return [], SCENE_VOXEL_SIZE

    voxel_size = _resolve_render_voxel_size(len(points))
    render_points = _aggregate_display_points(points, voxel_size)
    while len(render_points) > SCENE_RENDER_MAX_POINTS and voxel_size < SCENE_VOXEL_SIZE * 6:
        voxel_size *= 1.18
        render_points = _aggregate_display_points(points, voxel_size)

    if len(render_points) > SCENE_RENDER_MAX_POINTS:
        render_points = _select_points(render_points, trajectory, SCENE_RENDER_MAX_POINTS)

    return render_points, voxel_size


def _serialize_points(points: list[CloudPoint]) -> list[list[float]]:
    serialized: list[list[float]] = []
    for point in points:
        row = [round(point.x, 4), round(point.y, 4), round(point.z, 4), round(point.intensity, 2)]
        if point.rgb is not None:
            row.extend(
                [
                    round(_clamp(point.rgb[0], 0, 255), 1),
                    round(_clamp(point.rgb[1], 0, 255), 1),
                    round(_clamp(point.rgb[2], 0, 255), 1),
                ]
            )
        serialized.append(row)
    return serialized


def _compute_bounds(points: list[CloudPoint], trajectory: list[list[float]]) -> dict[str, list[float]]:
    if not points and not trajectory:
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}

    xs = [point.x for point in points] + [item[0] for item in trajectory]
    ys = [point.y for point in points] + [item[1] for item in trajectory]
    zs = [point.z for point in points] + [item[2] for item in trajectory]
    padding = [2.4, 2.4, 1.2]
    return {
        "min": [
            round(min(xs) - padding[0], 4),
            round(min(ys) - padding[1], 4),
            round(min(zs) - padding[2], 4),
        ],
        "max": [
            round(max(xs) + padding[0], 4),
            round(max(ys) + padding[1], 4),
            round(max(zs) + padding[2], 4),
        ],
    }


def _max_z(points: list[CloudPoint]) -> float:
    return max((point.z for point in points), default=0.0)


def _load_pcd_rows(path: Path) -> np.ndarray:
    with path.open("rb") as handle:
        header_lines: list[str] = []
        while True:
            line = handle.readline()
            if not line:
                raise ValueError(f"Incomplete PCD header: {path}")
            decoded = line.decode("ascii", errors="ignore").strip()
            if decoded:
                header_lines.append(decoded)
            if decoded.upper().startswith("DATA"):
                break
        metadata = _parse_pcd_header(header_lines, path)
        payload = handle.read()

    if metadata["data"] != "binary":
        raise ValueError(f"Only binary PCD is supported: {path}")

    dtype = metadata["dtype"]
    point_count = int(metadata["points"])
    structured = np.frombuffer(payload, dtype=dtype, count=point_count)
    x = structured["x"].astype(np.float32, copy=False)
    y = structured["y"].astype(np.float32, copy=False)
    z = structured["z"].astype(np.float32, copy=False)
    if "intensity" in structured.dtype.names:
        intensity = structured["intensity"].astype(np.float32, copy=False)
    else:
        intensity = np.zeros_like(x, dtype=np.float32)
    return np.column_stack((x, y, z, intensity)).astype(np.float32, copy=False)


def _parse_pcd_header(lines: list[str], path: Path) -> dict[str, object]:
    raw: dict[str, list[str]] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        raw[parts[0].upper()] = parts[1:]

    fields = raw.get("FIELDS")
    sizes = raw.get("SIZE")
    types = raw.get("TYPE")
    counts = raw.get("COUNT", ["1"] * len(fields or []))
    if not fields or not sizes or not types or not (len(fields) == len(sizes) == len(types) == len(counts)):
        raise ValueError(f"Invalid PCD field definition: {path}")

    names: list[str] = []
    formats: list[object] = []
    offsets: list[int] = []
    offset = 0
    for name, size, kind, count in zip(fields, sizes, types, counts):
        size_int = int(size)
        count_int = int(count)
        dtype = _numpy_dtype(kind, size_int)
        names.append(name)
        formats.append((dtype, (count_int,)) if count_int > 1 else dtype)
        offsets.append(offset)
        offset += size_int * count_int

    dtype = np.dtype({"names": names, "formats": formats, "offsets": offsets, "itemsize": offset})
    point_count = int(raw.get("POINTS", raw.get("WIDTH", ["0"]))[0])
    data_kind = raw.get("DATA", [""])[0].lower()
    return {"dtype": dtype, "points": point_count, "data": data_kind}


def _numpy_dtype(kind: str, size: int):
    mapping = {
        ("F", 4): np.float32,
        ("F", 8): np.float64,
        ("U", 1): np.uint8,
        ("U", 2): np.uint16,
        ("U", 4): np.uint32,
        ("I", 1): np.int8,
        ("I", 2): np.int16,
        ("I", 4): np.int32,
    }
    dtype = mapping.get((kind, size))
    if dtype is None:
        raise ValueError(f"Unsupported PCD field type: {(kind, size)}")
    return dtype


def _score_projection_attempt(
    pairs: list[PairFrame],
    calibration: CameraCalibration,
    rotation: np.ndarray,
    translation: np.ndarray,
    mode: str,
) -> ProjectionAttempt:
    if not pairs:
        return ProjectionAttempt(mode, rotation, translation, 0.0, 0.0, 0.0)

    total_points = 0
    positive_count = 0
    in_frame_count = 0

    sample_count = min(len(pairs), max(1, SCENE_CALIBRATION_SAMPLE_FRAMES))
    sample_indices = sorted({round(index * (len(pairs) - 1) / max(1, sample_count - 1)) for index in range(sample_count)})
    for index in sample_indices:
        pair = pairs[index]
        point_rows = load_point_rows(pair.point_path)
        if point_rows.size == 0:
            continue

        sampled = _subsample_rows(point_rows[:, :3], SCENE_CALIBRATION_SAMPLE_POINTS)
        image_size = _read_image_size(pair.image_path)
        if image_size is None:
            continue

        projected = sampled @ rotation.T + translation
        depth = projected[:, 2]
        positive_mask = depth > 0.1
        total = int(sampled.shape[0])
        positive = int(np.count_nonzero(positive_mask))
        total_points += total
        positive_count += positive
        if positive == 0:
            continue

        width, height = image_size
        u = calibration.camera_matrix[0, 0] * (projected[:, 0] / np.maximum(depth, 1e-6)) + calibration.camera_matrix[0, 2]
        v = calibration.camera_matrix[1, 1] * (projected[:, 1] / np.maximum(depth, 1e-6)) + calibration.camera_matrix[1, 2]
        valid = positive_mask & (u >= 0) & (u < width) & (v >= 0) & (v < height)
        in_frame_count += int(np.count_nonzero(valid))

    if total_points == 0:
        return ProjectionAttempt(mode, rotation, translation, 0.0, 0.0, 0.0)

    positive_ratio = positive_count / total_points
    in_frame_ratio = in_frame_count / total_points
    visible_ratio = in_frame_count / max(1, positive_count)
    score = in_frame_ratio * 0.7 + positive_ratio * 0.15 + visible_ratio * 0.15
    return ProjectionAttempt(mode, rotation, translation, score, positive_ratio, in_frame_ratio)


def _camera_matrix(calibration_path: Path) -> np.ndarray:
    cached = _CAMERA_MATRIX_CACHE.get(calibration_path)
    if cached is not None:
        return cached
    calibration = load_camera_calibration(calibration_path)
    _CAMERA_MATRIX_CACHE[calibration_path] = calibration.camera_matrix
    return calibration.camera_matrix


def _roof_layer_threshold(points: list[CloudPoint], z_values: list[float]) -> tuple[float | None, str]:
    search_floor = _percentile(z_values, 0.68)
    lower_guard = _percentile(z_values, 0.55)
    z_bin = max(0.16, min(0.28, SCENE_VOXEL_SIZE * 1.15))
    xy_bin = max(0.45, SCENE_VOXEL_SIZE * 2.8)

    counts: collections.Counter[float] = collections.Counter()
    coverage: dict[float, set[tuple[int, int]]] = collections.defaultdict(set)

    for point in points:
        if point.z < search_floor:
            continue
        z_key = round(math.floor(point.z / z_bin) * z_bin, 3)
        counts[z_key] += max(1, int(point.density))
        coverage[z_key].add((math.floor(point.x / xy_bin), math.floor(point.y / xy_bin)))

    if not counts:
        return None, "no upper-layer bins"

    peak_count = max(counts.values())
    peak_coverage = max(len(cells) for cells in coverage.values())
    max_bin_z = max(counts)
    candidates: list[tuple[float, float, int, int]] = []

    for z_key in sorted(counts):
        coverage_count = len(coverage[z_key])
        coverage_ratio = coverage_count / max(1, peak_coverage)
        count_ratio = counts[z_key] / max(1, peak_count)
        if coverage_ratio < 0.42 or count_ratio < 0.18:
            continue
        z_ratio = (z_key - search_floor) / max(0.001, max_bin_z - search_floor)
        score = coverage_ratio * 0.74 + count_ratio * 0.26 + z_ratio * 0.08
        candidates.append((score, z_key, counts[z_key], coverage_count))

    if not candidates:
        return None, "no dominant roof layer"

    _, peak_z, roof_count, roof_coverage = max(candidates, key=lambda item: item[0])
    ordered_bins = sorted(counts)
    peak_index = ordered_bins.index(peak_z)
    roof_start = peak_z

    for index in range(peak_index, -1, -1):
        current_z = ordered_bins[index]
        current_count = counts[current_z]
        current_coverage = len(coverage[current_z])
        if current_coverage >= roof_coverage * 0.72 and current_count >= roof_count * 0.45:
            roof_start = current_z
            continue
        break

    threshold = max(lower_guard, roof_start - z_bin * 0.75)
    return (
        threshold,
        (
            f"roof layer detected near z={peak_z:.2f} m with start {roof_start:.2f} m; "
            f"coverage bins={roof_coverage}, count={roof_count}"
        ),
    )


def _aggregate_display_points(points: list[CloudPoint], voxel_size: float) -> list[CloudPoint]:
    buckets: dict[tuple[int, int, int], list[float]] = {}
    for point in points:
        key = (
            int(math.floor(point.x / voxel_size)),
            int(math.floor(point.y / voxel_size)),
            int(math.floor(point.z / voxel_size)),
        )
        bucket = buckets.get(key)
        if bucket is None:
            bucket = [0.0] * 9
            buckets[key] = bucket

        weight = max(1.0, point.density)
        bucket[0] += point.x * weight
        bucket[1] += point.y * weight
        bucket[2] += point.z * weight
        bucket[3] += point.intensity * weight
        bucket[4] += weight
        if point.rgb is not None:
            bucket[5] += point.rgb[0] * weight
            bucket[6] += point.rgb[1] * weight
            bucket[7] += point.rgb[2] * weight
            bucket[8] += weight

    aggregated: list[CloudPoint] = []
    for values in buckets.values():
        weight = max(1.0, values[4])
        rgb = None
        if values[8] > 0:
            rgb = (values[5] / values[8], values[6] / values[8], values[7] / values[8])
        aggregated.append(
            CloudPoint(
                x=values[0] / weight,
                y=values[1] / weight,
                z=values[2] / weight,
                intensity=values[3] / weight,
                density=weight,
                rgb=rgb,
            )
        )
    return aggregated


def _resolve_render_voxel_size(raw_count: int) -> float:
    ratio = max(1.0, raw_count / max(1, SCENE_RENDER_TARGET_POINTS))
    factor = math.cbrt(ratio)
    return _clamp(SCENE_VOXEL_SIZE * factor * 1.08, SCENE_VOXEL_SIZE * 1.0, SCENE_VOXEL_SIZE * 3.4)


def _sample_vectors(points: list[list[float]], max_samples: int) -> list[tuple[float, float, float]]:
    if not points:
        return []
    if len(points) <= max_samples:
        return [(point[0], point[1], point[2]) for point in points]
    samples: list[tuple[float, float, float]] = []
    for index in range(max_samples):
        source_index = round(index * (len(points) - 1) / (max_samples - 1))
        point = points[source_index]
        samples.append((point[0], point[1], point[2]))
    return samples


def _nearest_trajectory_distance_sq(point: CloudPoint, trajectory_samples: list[tuple[float, float, float]]) -> float:
    if not trajectory_samples:
        return 0.0
    return min((point.x - tx) ** 2 + (point.y - ty) ** 2 + (point.z - tz) ** 2 for tx, ty, tz in trajectory_samples)


def _rotation_matrix_from_quaternion(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    return np.asarray(
        [
            [1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)],
            [2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)],
            [2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)],
        ],
        dtype=np.float64,
    )


def _read_image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def _subsample_rows(rows: np.ndarray, max_points: int) -> np.ndarray:
    if rows.shape[0] <= max_points:
        return rows
    indices = np.linspace(0, rows.shape[0] - 1, num=max_points, dtype=np.int64)
    return rows[indices]


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, round((len(values) - 1) * quantile)))
    return float(values[index])


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
