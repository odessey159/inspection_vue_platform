from __future__ import annotations

from datetime import datetime, timezone
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlmodel import Session

from ..models import Project
from ..settings import COLMAP_BIN, SECURITY_CHECK_CALIBRATION_PATH
from .dataset import PairFrame, PoseRecord, parse_pairs_csv, parse_pose_csv, sample_trajectory_with_orientations
from .extractors import export_camera_lidar_pairs, export_pose_calibration
from .import_pipeline import _resolve_pose_path, project_runtime_paths
from .rosbag import is_rosbag_dir
from .scene import (
    CloudPoint,
    _build_render_points,
    _compute_bounds,
    _load_lidar_mount_transform,
    _max_z,
    _resolve_colorization,
    _roof_threshold,
    _select_points,
    _serialize_points,
    _trim_vertical_outliers,
)
from .storage import read_json, remove_paths, write_json


FRAME_INTERVAL_MS = 1000
FRAME_MIN_DISTANCE_M = 0.35
FRAME_MIN_YAW_DELTA_RAD = math.radians(8.0)
FRAME_TARGET_MIN = 300
FRAME_TARGET_MAX = 800
FRAME_HARD_MIN = 250
REGISTERED_IMAGE_MIN = 120
ALIGNMENT_INLIER_MIN = 60
ALIGNMENT_RMSE_MAX_M = 1.5
PATCH_MATCH_MAX_IMAGE_SIZE = 1280
SEQUENTIAL_MATCH_OVERLAP = 10


@dataclass(slots=True)
class SelectedImage:
    pair: PairFrame
    pose: PoseRecord
    ros_camera_center: np.ndarray
    export_name: str


@dataclass(slots=True)
class SimilarityTransform:
    scale: float
    rotation: np.ndarray
    translation: np.ndarray
    rmse: float
    inlier_count: int
    total_count: int


def rebuild_project_sfm_scene(session: Session, project: Project) -> dict[str, object]:
    if project.id is None:
        raise ValueError("Project id is missing")

    colmap_path = _resolve_colmap_bin()
    if colmap_path is None:
        raise FileNotFoundError(
            "COLMAP is not installed or not configured. Set COLMAP_BIN or add colmap.exe to PATH."
        )

    bag_dir = Path(project.bag_dir).resolve()
    if not is_rosbag_dir(bag_dir):
        raise FileNotFoundError(f"Not a valid rosbag directory: {bag_dir}")

    runtime_paths = project_runtime_paths(project)
    runtime_paths["pairs_dir"].mkdir(parents=True, exist_ok=True)
    runtime_paths["pose_dir"].mkdir(parents=True, exist_ok=True)

    if not (runtime_paths["pose_dir"] / "summary.json").exists():
        export_pose_calibration(bag_dir=bag_dir, output_dir=runtime_paths["pose_dir"])

    export_camera_lidar_pairs(
        bag_dir=bag_dir,
        output_dir=runtime_paths["pairs_dir"],
        image_topic=project.video_topic or "/cam/security_check",
        point_topic=project.point_topic or "/lidar/data",
        overwrite=True,
    )

    pairs = parse_pairs_csv(runtime_paths["pairs_dir"] / "pairs.csv")
    pose_path, pose_topic = _resolve_pose_path(runtime_paths["pose_dir"])
    poses = parse_pose_csv(pose_path)
    tf_static_path = runtime_paths["pose_dir"] / "tf_static.csv"

    selected_images = _select_images_for_sfm(pairs, poses, tf_static_path)
    if len(selected_images) < FRAME_HARD_MIN:
        raise ValueError(
            f"Only {len(selected_images)} candidate images passed the sampling thresholds; need at least {FRAME_HARD_MIN}."
        )

    project_root = Path(project.artifacts_dir)
    workspace_root = project_root / "tmp" / "sfm_experiment"
    images_dir = workspace_root / "images"
    sparse_dir = workspace_root / "sparse"
    sparse_txt_root = workspace_root / "sparse_txt"
    dense_dir = workspace_root / "dense"
    database_path = workspace_root / "colmap.db"
    summary_path = project_root / "summaries" / "sfm_summary.json"
    scene_path = project_root / "scenes" / "scene_sfm.json"

    diagnostics: list[str] = []

    try:
        remove_paths([workspace_root])
        images_dir.mkdir(parents=True, exist_ok=True)
        sparse_dir.mkdir(parents=True, exist_ok=True)
        sparse_txt_root.mkdir(parents=True, exist_ok=True)
        dense_dir.mkdir(parents=True, exist_ok=True)

        selected_map = _materialize_selected_images(selected_images, images_dir)
        camera_params = _camera_params_string()

        _run_colmap(
            colmap_path,
            [
                "feature_extractor",
                "--database_path",
                str(database_path),
                "--image_path",
                str(images_dir),
                "--ImageReader.camera_model",
                "PINHOLE",
                "--ImageReader.single_camera",
                "1",
                "--ImageReader.camera_params",
                camera_params,
                "--SiftExtraction.use_gpu",
                "1",
            ],
        )
        diagnostics.append("COLMAP feature extraction completed.")

        _run_colmap(
            colmap_path,
            [
                "sequential_matcher",
                "--database_path",
                str(database_path),
                "--SequentialMatching.overlap",
                str(SEQUENTIAL_MATCH_OVERLAP),
                "--SiftMatching.use_gpu",
                "1",
            ],
        )
        diagnostics.append("COLMAP sequential matching completed.")

        _run_colmap(
            colmap_path,
            [
                "mapper",
                "--database_path",
                str(database_path),
                "--image_path",
                str(images_dir),
                "--output_path",
                str(sparse_dir),
            ],
        )
        diagnostics.append("COLMAP sparse mapping completed.")

        best_model_dir, images_txt = _export_best_sparse_model(colmap_path, sparse_dir, sparse_txt_root)
        sparse_poses = _parse_colmap_images_txt(images_txt)
        registered_image_count = len(sparse_poses)
        if registered_image_count < REGISTERED_IMAGE_MIN:
            raise ValueError(
                f"COLMAP registered only {registered_image_count} images; need at least {REGISTERED_IMAGE_MIN}."
            )

        similarity = _align_sparse_model(sparse_poses, selected_map)
        if similarity.inlier_count < ALIGNMENT_INLIER_MIN:
            raise ValueError(
                f"Only {similarity.inlier_count} alignment inliers were found; need at least {ALIGNMENT_INLIER_MIN}."
            )
        if similarity.rmse > ALIGNMENT_RMSE_MAX_M:
            raise ValueError(
                f"Alignment RMSE is {similarity.rmse:.3f} m; must be <= {ALIGNMENT_RMSE_MAX_M:.3f} m."
            )
        diagnostics.append(
            f"ROS alignment succeeded with {similarity.inlier_count}/{similarity.total_count} inliers, RMSE {similarity.rmse:.3f} m."
        )

        _run_colmap(
            colmap_path,
            [
                "image_undistorter",
                "--image_path",
                str(images_dir),
                "--input_path",
                str(best_model_dir),
                "--output_path",
                str(dense_dir),
                "--output_type",
                "COLMAP",
            ],
        )
        diagnostics.append("COLMAP image undistortion completed.")

        _run_colmap(
            colmap_path,
            [
                "patch_match_stereo",
                "--workspace_path",
                str(dense_dir),
                "--workspace_format",
                "COLMAP",
                "--PatchMatchStereo.geom_consistency",
                "true",
                "--PatchMatchStereo.max_image_size",
                str(PATCH_MATCH_MAX_IMAGE_SIZE),
            ],
        )
        diagnostics.append("COLMAP dense stereo completed.")

        fused_path = dense_dir / "fused.ply"
        _run_colmap(
            colmap_path,
            [
                "stereo_fusion",
                "--workspace_path",
                str(dense_dir),
                "--workspace_format",
                "COLMAP",
                "--input_type",
                "geometric",
                "--output_path",
                str(fused_path),
            ],
        )
        diagnostics.append("COLMAP stereo fusion completed.")

        dense_points = _load_ply_points(fused_path)
        if not dense_points:
            raise ValueError("COLMAP dense fusion produced an empty point cloud.")

        transformed_points = _transform_cloud_points(dense_points, similarity)
        scene_payload = _build_sfm_scene_payload(
            transformed_points=transformed_points,
            poses=poses,
            pose_topic=pose_topic,
            selected_image_count=len(selected_images),
            registered_image_count=registered_image_count,
            alignment_rmse=similarity.rmse,
        )
        write_json(scene_path, scene_payload)

        summary_payload = {
            "status": "ready",
            "scene_source": "sfm",
            "reconstruction_method": "colmap_sfm_mvs",
            "selected_image_count": len(selected_images),
            "registered_image_count": registered_image_count,
            "dense_point_count": len(dense_points),
            "aligned": True,
            "alignment_rmse_m": round(similarity.rmse, 4),
            "notes": scene_payload["notes"],
            "diagnostics": diagnostics,
            "colmap_bin": str(colmap_path),
        }
        write_json(summary_path, summary_payload)

        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()

        return {
            "selected_image_count": len(selected_images),
            "registered_image_count": registered_image_count,
            "dense_point_count": len(dense_points),
            "alignment_rmse_m": similarity.rmse,
            "notes": scene_payload["notes"],
        }
    except Exception as exc:
        failure_payload = {
            "status": "failed",
            "scene_source": "sfm",
            "aligned": False,
            "notes": [str(exc)],
            "diagnostics": diagnostics,
            "colmap_bin": str(colmap_path),
        }
        write_json(summary_path, failure_payload)
        raise
    finally:
        remove_paths([workspace_root])
        remove_paths([runtime_paths["pairs_dir"]])


def load_sfm_summary(project: Project) -> dict[str, object]:
    summary_path = Path(project.artifacts_dir) / "summaries" / "sfm_summary.json"
    if not summary_path.exists():
        return {}
    return read_json(summary_path)


def sfm_scene_path(project: Project) -> Path:
    return Path(project.artifacts_dir) / "scenes" / "scene_sfm.json"


def sfm_scene_exists(project: Project) -> bool:
    return sfm_scene_path(project).exists()


def _resolve_colmap_bin() -> Path | None:
    if COLMAP_BIN:
        path = Path(COLMAP_BIN)
        if path.exists():
            return path

    from_path = shutil.which("colmap")
    if from_path:
        return Path(from_path)

    candidates = [
        Path("C:/Program Files/COLMAP/colmap.exe"),
        Path("C:/Program Files/colmap/colmap.exe"),
        Path("C:/Program Files/COLMAP/bin/colmap.exe"),
        Path("C:/Program Files/colmap/bin/colmap.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _select_images_for_sfm(pairs: list[PairFrame], poses: list[PoseRecord], tf_static_path: Path) -> list[SelectedImage]:
    lidar_mount = _load_lidar_mount_transform(tf_static_path)
    colorization = _resolve_colorization(pairs, SECURITY_CHECK_CALIBRATION_PATH, [])
    if not colorization.enabled or colorization.rotation is None or colorization.translation is None:
        raise ValueError("Unable to resolve lidar-to-camera transform for ROS/SfM alignment.")

    camera_center_lidar = -colorization.rotation.T @ colorization.translation
    camera_center_base = lidar_mount.rotation @ camera_center_lidar + lidar_mount.translation

    selected: list[SelectedImage] = []
    last_pair: PairFrame | None = None
    last_pose: PoseRecord | None = None

    for pair in pairs:
        pose = min(poses, key=lambda item: abs(item.timestamp_ms - pair.image_timestamp_ms))
        if last_pair is None or last_pose is None:
            selected.append(_to_selected_image(pair, pose, camera_center_base, len(selected)))
            last_pair = pair
            last_pose = pose
            continue

        elapsed_ms = pair.image_timestamp_ms - last_pair.image_timestamp_ms
        distance_m = math.sqrt((pose.x - last_pose.x) ** 2 + (pose.y - last_pose.y) ** 2 + (pose.z - last_pose.z) ** 2)
        yaw_delta = _angle_delta(last_pose.yaw, pose.yaw)
        if elapsed_ms >= FRAME_INTERVAL_MS and (distance_m >= FRAME_MIN_DISTANCE_M or yaw_delta >= FRAME_MIN_YAW_DELTA_RAD):
            selected.append(_to_selected_image(pair, pose, camera_center_base, len(selected)))
            last_pair = pair
            last_pose = pose

    if len(selected) > FRAME_TARGET_MAX:
        indices = np.linspace(0, len(selected) - 1, num=FRAME_TARGET_MAX, dtype=np.int64)
        selected = [selected[int(index)] for index in indices]

    return selected


def _to_selected_image(pair: PairFrame, pose: PoseRecord, camera_center_base: np.ndarray, index: int) -> SelectedImage:
    pose_rotation = _pose_rotation(pose)
    ros_camera_center = pose_rotation @ camera_center_base + np.asarray([pose.x, pose.y, pose.z], dtype=np.float64)
    export_name = f"{pair.image_timestamp_ms:013d}_{index:04d}{pair.image_path.suffix.lower()}"
    return SelectedImage(
        pair=pair,
        pose=pose,
        ros_camera_center=ros_camera_center,
        export_name=export_name,
    )


def _camera_params_string() -> str:
    from .calibration import load_camera_calibration

    calibration = load_camera_calibration(SECURITY_CHECK_CALIBRATION_PATH)
    fx = float(calibration.camera_matrix[0, 0])
    fy = float(calibration.camera_matrix[1, 1])
    cx = float(calibration.camera_matrix[0, 2])
    cy = float(calibration.camera_matrix[1, 2])
    return f"{fx:.8f},{fy:.8f},{cx:.8f},{cy:.8f}"


def _materialize_selected_images(selected_images: list[SelectedImage], images_dir: Path) -> dict[str, SelectedImage]:
    selected_map: dict[str, SelectedImage] = {}
    for item in selected_images:
        target = images_dir / item.export_name
        shutil.copy2(item.pair.image_path, target)
        selected_map[item.export_name] = item
    return selected_map


def _run_colmap(colmap_path: Path, args: list[str]) -> None:
    command = [str(colmap_path), *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "COLMAP command failed"
        raise RuntimeError(message)


def _export_best_sparse_model(colmap_path: Path, sparse_dir: Path, sparse_txt_root: Path) -> tuple[Path, Path]:
    candidates = [path for path in sparse_dir.iterdir() if path.is_dir()]
    if not candidates:
        raise ValueError("COLMAP mapper did not produce any sparse model.")

    best: tuple[int, Path, Path] | None = None
    for candidate in candidates:
        txt_dir = sparse_txt_root / candidate.name
        txt_dir.mkdir(parents=True, exist_ok=True)
        _run_colmap(
            colmap_path,
            [
                "model_converter",
                "--input_path",
                str(candidate),
                "--output_path",
                str(txt_dir),
                "--output_type",
                "TXT",
            ],
        )
        images_txt = txt_dir / "images.txt"
        image_count = len(_parse_colmap_images_txt(images_txt))
        if best is None or image_count > best[0]:
            best = (image_count, candidate, images_txt)

    if best is None:
        raise ValueError("Unable to choose a sparse COLMAP model.")
    return best[1], best[2]


def _parse_colmap_images_txt(path: Path) -> dict[str, np.ndarray]:
    centers: dict[str, np.ndarray] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 10:
                continue
            if len(parts) >= 10 and parts[0].isdigit():
                image_name = parts[9]
                qw, qx, qy, qz = (float(parts[index]) for index in range(1, 5))
                tx, ty, tz = (float(parts[index]) for index in range(5, 8))
                rotation = _quat_to_rotation(qx, qy, qz, qw)
                translation = np.asarray([tx, ty, tz], dtype=np.float64)
                center = -rotation.T @ translation
                centers[image_name] = center
                next(handle, None)
    return centers


def _align_sparse_model(sparse_poses: dict[str, np.ndarray], selected_map: dict[str, SelectedImage]) -> SimilarityTransform:
    image_names = [name for name in sparse_poses if name in selected_map]
    if len(image_names) < 3:
        raise ValueError("Not enough image correspondences are available for ROS alignment.")

    colmap_points = np.stack([sparse_poses[name] for name in image_names], axis=0)
    ros_points = np.stack([selected_map[name].ros_camera_center for name in image_names], axis=0)
    return _estimate_similarity_ransac(colmap_points, ros_points)


def _estimate_similarity_ransac(source: np.ndarray, target: np.ndarray) -> SimilarityTransform:
    total = source.shape[0]
    rng = np.random.default_rng(7)
    best_inliers: np.ndarray | None = None
    best_rmse = float("inf")

    iterations = 160
    threshold = 0.9
    for _ in range(iterations):
        sample = rng.choice(total, size=3, replace=False)
        transform = _estimate_similarity(source[sample], target[sample])
        residuals = _similarity_residuals(source, target, transform)
        inliers = residuals <= threshold
        inlier_count = int(np.count_nonzero(inliers))
        if inlier_count < 3:
            continue
        rmse = float(np.sqrt(np.mean(np.square(residuals[inliers]))))
        if best_inliers is None or inlier_count > int(np.count_nonzero(best_inliers)) or (
            inlier_count == int(np.count_nonzero(best_inliers)) and rmse < best_rmse
        ):
            best_inliers = inliers
            best_rmse = rmse

    if best_inliers is None:
        raise ValueError("Failed to estimate a stable ROS/SfM alignment.")

    refined = _estimate_similarity(source[best_inliers], target[best_inliers])
    residuals = _similarity_residuals(source, target, refined)
    inliers = residuals <= threshold
    rmse = float(np.sqrt(np.mean(np.square(residuals[inliers]))))
    return SimilarityTransform(
        scale=refined.scale,
        rotation=refined.rotation,
        translation=refined.translation,
        rmse=rmse,
        inlier_count=int(np.count_nonzero(inliers)),
        total_count=total,
    )


def _estimate_similarity(source: np.ndarray, target: np.ndarray) -> SimilarityTransform:
    mu_source = source.mean(axis=0)
    mu_target = target.mean(axis=0)
    source_centered = source - mu_source
    target_centered = target - mu_target

    covariance = (target_centered.T @ source_centered) / max(1, source.shape[0])
    u, singular_values, vt = np.linalg.svd(covariance)
    correction = np.eye(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        correction[2, 2] = -1
    rotation = u @ correction @ vt

    source_variance = np.mean(np.sum(source_centered * source_centered, axis=1))
    scale = float(np.trace(np.diag(singular_values) @ correction) / max(source_variance, 1e-9))
    translation = mu_target - scale * (rotation @ mu_source)
    return SimilarityTransform(
        scale=scale,
        rotation=rotation,
        translation=translation,
        rmse=0.0,
        inlier_count=source.shape[0],
        total_count=source.shape[0],
    )


def _similarity_residuals(source: np.ndarray, target: np.ndarray, transform: SimilarityTransform) -> np.ndarray:
    predicted = (transform.scale * (transform.rotation @ source.T)).T + transform.translation
    return np.linalg.norm(predicted - target, axis=1)


def _load_ply_points(path: Path) -> list[CloudPoint]:
    with path.open("rb") as handle:
        header_lines: list[str] = []
        while True:
            line = handle.readline()
            if not line:
                raise ValueError(f"Incomplete PLY header: {path}")
            decoded = line.decode("ascii", errors="ignore").strip()
            header_lines.append(decoded)
            if decoded == "end_header":
                break
        header_text = "\n".join(header_lines)

        if "format binary_little_endian 1.0" not in header_text:
            raise ValueError("Only binary little-endian PLY output is supported.")

        vertex_count = 0
        properties: list[tuple[str, str]] = []
        in_vertex = False
        for line in header_lines:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "element" and parts[1] == "vertex":
                vertex_count = int(parts[2])
                in_vertex = True
                continue
            if in_vertex and len(parts) >= 3 and parts[0] == "property":
                properties.append((parts[1], parts[2]))
                continue
            if in_vertex and parts[:1] == ["element"]:
                break

        if vertex_count <= 0:
            raise ValueError("PLY output does not contain any vertices.")

        dtype = _ply_dtype(properties)
        payload = handle.read()
        structured = np.frombuffer(payload, dtype=dtype, count=vertex_count)

    points: list[CloudPoint] = []
    for row in structured:
        rgb = None
        if {"red", "green", "blue"}.issubset(structured.dtype.names or []):
            rgb = (float(row["red"]), float(row["green"]), float(row["blue"]))
        points.append(
            CloudPoint(
                x=float(row["x"]),
                y=float(row["y"]),
                z=float(row["z"]),
                intensity=0.0,
                density=1.0,
                rgb=rgb,
            )
        )
    return points


def _ply_dtype(properties: list[tuple[str, str]]) -> np.dtype:
    type_map = {
        "float": np.float32,
        "float32": np.float32,
        "double": np.float64,
        "uchar": np.uint8,
        "uint8": np.uint8,
        "int": np.int32,
        "int32": np.int32,
    }
    names: list[str] = []
    formats: list[object] = []
    for type_name, property_name in properties:
        dtype = type_map.get(type_name)
        if dtype is None:
            raise ValueError(f"Unsupported PLY property type: {type_name}")
        names.append(property_name)
        formats.append(dtype)
    return np.dtype({"names": names, "formats": formats})


def _transform_cloud_points(points: list[CloudPoint], transform: SimilarityTransform) -> list[CloudPoint]:
    transformed: list[CloudPoint] = []
    for point in points:
        vector = np.asarray([point.x, point.y, point.z], dtype=np.float64)
        mapped = transform.scale * (transform.rotation @ vector) + transform.translation
        transformed.append(
            CloudPoint(
                x=float(mapped[0]),
                y=float(mapped[1]),
                z=float(mapped[2]),
                intensity=point.intensity,
                density=point.density,
                rgb=point.rgb,
            )
        )
    return transformed


def _build_sfm_scene_payload(
    *,
    transformed_points: list[CloudPoint],
    poses: list[PoseRecord],
    pose_topic: str,
    selected_image_count: int,
    registered_image_count: int,
    alignment_rmse: float,
) -> dict[str, object]:
    trajectory, trajectory_timestamps, trajectory_orientations = sample_trajectory_with_orientations(poses)
    full_candidates = _trim_vertical_outliers(transformed_points)
    roof_threshold, roof_context = _roof_threshold(full_candidates)
    roof_points = [point for point in full_candidates if point.z <= roof_threshold]
    active_points = roof_points or full_candidates

    source_points = _select_points(active_points, trajectory, max_points=180000)
    render_points_raw, _ = _build_render_points(active_points, trajectory)
    render_points = _serialize_points(render_points_raw)

    return {
        "scene_source": "sfm",
        "reconstruction_method": "colmap_sfm_mvs",
        "points": _serialize_points(source_points),
        "full_points": _serialize_points(full_candidates),
        "roof_removed_points": _serialize_points(active_points),
        "render_points": render_points,
        "default_point_mode": "roof_removed",
        "trajectory": trajectory,
        "trajectory_timestamps": trajectory_timestamps,
        "trajectory_orientations": trajectory_orientations,
        "bounds": _compute_bounds(active_points, trajectory),
        "full_bounds": _compute_bounds(full_candidates, trajectory),
        "roof_removed_bounds": _compute_bounds(active_points, trajectory),
        "source_frame_count": selected_image_count,
        "coordinate_frame": "global",
        "source_type": "colmap_dense_scene",
        "raw_point_count": len(active_points),
        "render_point_count": len(render_points),
        "colorized": True,
        "color_source": "colmap_dense_rgb",
        "cut_height_default": round(float(min(roof_threshold, _max_z(active_points))), 4),
        "selected_image_count": selected_image_count,
        "registered_image_count": registered_image_count,
        "alignment_status": "aligned",
        "alignment_rmse_m": round(alignment_rmse, 4),
        "notes": [
            f"COLMAP image reconstruction selected {selected_image_count} images and registered {registered_image_count}.",
            f"Scene aligned to {pose_topic} with RMSE {alignment_rmse:.3f} m.",
            f"Roof-off cut height: z={roof_threshold:.2f} m ({roof_context}).",
            "Dense points come directly from COLMAP stereo fusion and carry image RGB colors.",
        ],
    }


def _pose_rotation(pose: PoseRecord) -> np.ndarray:
    xx, yy, zz = pose.qx * pose.qx, pose.qy * pose.qy, pose.qz * pose.qz
    xy, xz, yz = pose.qx * pose.qy, pose.qx * pose.qz, pose.qy * pose.qz
    wx, wy, wz = pose.qw * pose.qx, pose.qw * pose.qy, pose.qw * pose.qz
    return np.asarray(
        [
            [1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)],
            [2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)],
            [2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)],
        ],
        dtype=np.float64,
    )


def _quat_to_rotation(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
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


def _angle_delta(previous: float, current: float) -> float:
    delta = current - previous
    while delta > math.pi:
        delta -= 2 * math.pi
    while delta < -math.pi:
        delta += 2 * math.pi
    return abs(delta)
