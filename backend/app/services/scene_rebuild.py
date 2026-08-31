from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

from sqlmodel import Session

from ..models import Project
from ..settings import SECURITY_CHECK_CALIBRATION_PATH
from .dataset import build_dataset_summary, parse_pairs_csv, parse_pose_csv
from .extractors import export_camera_lidar_pairs, export_pose_calibration
from .import_pipeline import _resolve_pose_path, _resolve_pose_validity_path, project_runtime_paths
from .maps import import_payload_as_map
from .rosbag import is_rosbag_dir
from .runtime import compact_project_runtime
from .scene import build_scene
from .storage import write_json


def rebuild_project_scene(session: Session, project: Project) -> dict[str, object]:
    if project.id is None:
        raise ValueError("Project id is missing")

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
    dataset_summary = build_dataset_summary(pairs)
    pose_path, pose_topic = _resolve_pose_path(runtime_paths["pose_dir"])
    poses = parse_pose_csv(pose_path)
    valid_pose_path, valid_pose_source = _resolve_pose_validity_path(runtime_paths["pose_dir"])
    valid_pose_reference = parse_pose_csv(valid_pose_path) if valid_pose_path is not None else None

    with tempfile.TemporaryDirectory(prefix="map-rebuild-") as tmp:
        scene_path = Path(tmp) / "scene.json"
        scene_payload = build_scene(
            pairs,
            poses,
            scene_path,
            pose_topic=pose_topic,
            calibration_path=SECURITY_CHECK_CALIBRATION_PATH,
            tf_static_path=runtime_paths["pose_dir"] / "tf_static.csv",
            valid_pose_reference=valid_pose_reference,
            valid_pose_source=valid_pose_source,
        )

    record = import_payload_as_map(scene_payload, name=f"{project.name}-lidar")
    notes = list(scene_payload.get("notes") or [])
    notes.append(f"已写入独立地图目录，map_id={record.id}。请在小车上绑定该索引后显示。")
    scene_payload["notes"] = notes

    write_json(runtime_paths["dataset_summary"], dataset_summary)

    project.pose_topic = pose_topic
    project.time_offset_ms = int(dataset_summary["time_offset_ms"])
    project.updated_at = datetime.now(timezone.utc)
    session.add(project)
    session.commit()
    session.refresh(project)

    compact_project_runtime(Path(project.artifacts_dir))

    return {
        "project": project,
        "scene": scene_payload,
        "map": record.payload(),
    }
