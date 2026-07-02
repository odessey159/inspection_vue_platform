from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from ..models import Project
from ..settings import DEFAULT_STANDARDS_DIR, DISCOVERY_ROOTS, SECURITY_CHECK_CALIBRATION_PATH, SUPPORTED_VISION_MODELS, VISION_MODEL
from .dataset import build_dataset_summary, parse_pairs_csv, parse_pose_csv
from .extractors import export_camera_lidar_pairs, export_pose_calibration
from .provider import provider_available
from .rosbag import is_rosbag_dir, parse_rosbag_summary
from .runtime import compact_project_runtime
from .rules import export_rules_payload, parse_rules
from .scene import build_scene
from .storage import ensure_project_dirs, write_json
from .video import build_video


def bootstrap_paths() -> dict[str, object]:
    bag_dirs = _discover_bag_dirs()
    standards_dirs = _discover_standards_dirs()
    preferred_standards = _preferred_standards_dir(standards_dirs)
    return {
        "sample_bag_dir": bag_dirs[0] if bag_dirs else None,
        "sample_standards_dir": preferred_standards,
        "detected_bag_dirs": bag_dirs,
        "detected_standards_dirs": standards_dirs,
        "provider_available": provider_available(),
        "default_analysis_model": VISION_MODEL,
        "supported_analysis_models": SUPPORTED_VISION_MODELS,
    }


def list_projects(session: Session) -> list[Project]:
    return list(session.exec(select(Project).order_by(Project.created_at.desc())))


def import_project(session: Session, name: str, bag_dir: Path, standards_dir: Path) -> Project:
    bag_dir = bag_dir.resolve()
    standards_dir = standards_dir.resolve()

    if not is_rosbag_dir(bag_dir):
        raise FileNotFoundError(f"Not a valid rosbag directory: {bag_dir}")
    if not standards_dir.exists() or not standards_dir.is_dir():
        raise FileNotFoundError(f"Standards directory not found: {standards_dir}")

    project = Project(
        name=name,
        status="indexing",
        bag_dir=str(bag_dir),
        standards_dir=str(standards_dir),
        artifacts_dir="",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    if project.id is None:
        raise RuntimeError("Project creation failed")

    project_dirs = ensure_project_dirs(project.id)
    project.artifacts_dir = str(project_dirs["root"])

    try:
        summary = parse_rosbag_summary(bag_dir)
        preferred = summary.get("preferred_topics", {})
        video_topic = preferred.get("video") or "/cam/security_check"
        point_topic = preferred.get("pointcloud") or "/lidar/data"

        extracted_pairs_dir = project_dirs["root"] / "extracted" / "camera_lidar_pairs"
        extracted_pose_dir = project_dirs["root"] / "extracted" / "pose_calibration"

        export_camera_lidar_pairs(
            bag_dir=bag_dir,
            output_dir=extracted_pairs_dir,
            image_topic=video_topic,
            point_topic=point_topic,
        )
        export_pose_calibration(bag_dir=bag_dir, output_dir=extracted_pose_dir)

        pairs = parse_pairs_csv(extracted_pairs_dir / "pairs.csv")
        dataset_summary = build_dataset_summary(pairs)
        pose_path, pose_topic = _resolve_pose_path(extracted_pose_dir)

        poses = parse_pose_csv(pose_path)
        valid_pose_path, valid_pose_source = _resolve_pose_validity_path(extracted_pose_dir)
        valid_pose_reference = parse_pose_csv(valid_pose_path) if valid_pose_path is not None else None
        rules = parse_rules(standards_dir, project.id)

        rosbag_summary_path = project_dirs["summaries"] / "rosbag_summary.json"
        dataset_summary_path = project_dirs["summaries"] / "dataset_summary.json"
        rules_path = project_dirs["summaries"] / "rules.json"
        scene_path = project_dirs["scenes"] / "scene.json"
        video_manifest_path = project_dirs["manifests"] / "video_manifest.json"
        video_output_path = project_dirs["artifacts"] / "inspection.mp4"

        build_scene(
            pairs,
            poses,
            scene_path,
            pose_topic=pose_topic,
            calibration_path=SECURITY_CHECK_CALIBRATION_PATH,
            tf_static_path=extracted_pose_dir / "tf_static.csv",
            valid_pose_reference=valid_pose_reference,
            valid_pose_source=valid_pose_source,
        )
        build_video(
            [pair.image_path for pair in pairs],
            [pair.image_timestamp_ms for pair in pairs],
            video_output_path,
            video_manifest_path,
            float(dataset_summary["inferred_fps"]),
        )

        write_json(rosbag_summary_path, summary)
        write_json(dataset_summary_path, dataset_summary)
        write_json(rules_path, export_rules_payload(rules))

        for rule in rules:
            session.add(rule)

        project.status = "indexed"
        project.video_topic = video_topic
        project.point_topic = point_topic
        project.pose_topic = pose_topic
        project.bag_start_ts = int(summary.get("start_ts_ms") or dataset_summary["video_start_ts"])
        project.bag_end_ts = int(summary.get("end_ts_ms") or dataset_summary["video_end_ts"])
        project.bag_duration_ms = int(summary.get("duration_ms") or (dataset_summary["video_end_ts"] - dataset_summary["video_start_ts"]))
        project.message_count = int(summary.get("message_count") or 0)
        project.rules_count = len(rules)
        project.findings_count = 0
        project.calibration_required = False
        project.time_offset_ms = int(dataset_summary["time_offset_ms"])
        project.rosbag_summary_path = str(rosbag_summary_path)
        project.rules_path = str(rules_path)
        project.scene_path = str(scene_path)
        project.inspection_video_path = str(video_output_path)
        project.updated_at = datetime.now(timezone.utc)

        session.add(project)
        session.commit()
        compact_project_runtime(project_dirs["root"])
        session.refresh(project)
        return project
    except Exception:
        project.status = "failed"
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()
        raise


def project_runtime_paths(project: Project) -> dict[str, Path]:
    root = Path(project.artifacts_dir)
    return {
        "root": root,
        "pairs_dir": root / "extracted" / "camera_lidar_pairs",
        "pose_dir": root / "extracted" / "pose_calibration",
        "dataset_summary": root / "summaries" / "dataset_summary.json",
        "video_manifest": root / "manifests" / "video_manifest.json",
        "evidence_frames": root / "evidence_frames",
    }


def _resolve_pose_path(pose_dir: Path) -> tuple[Path, str]:
    fusion_path = pose_dir / "fusion_odom.csv"
    lidar_slam_path = pose_dir / "lidar_slam_pose.csv"
    if fusion_path.exists() and _csv_has_rows(fusion_path):
        return fusion_path, "/fusion_odom"
    if lidar_slam_path.exists() and _csv_has_rows(lidar_slam_path):
        return lidar_slam_path, "/lidar_slam_pose"
    raise FileNotFoundError("No usable pose export was generated from /fusion_odom or /lidar_slam_pose")


def _resolve_pose_validity_path(pose_dir: Path) -> tuple[Path | None, str]:
    lidar_slam_path = pose_dir / "lidar_slam_pose.csv"
    if lidar_slam_path.exists() and _csv_has_rows(lidar_slam_path):
        return lidar_slam_path, "/lidar_slam_pose"
    return None, "primary pose timeline"


def _csv_has_rows(path: Path) -> bool:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle if line.strip()]
    return len(lines) > 1


def _discover_bag_dirs() -> list[str]:
    return _discover_directories(lambda path: is_rosbag_dir(path))


def _discover_standards_dirs() -> list[str]:
    return _discover_directories(_is_standards_dir)


def _preferred_standards_dir(candidates: list[str]) -> str | None:
    preferred = str(DEFAULT_STANDARDS_DIR.resolve())
    if DEFAULT_STANDARDS_DIR.exists() and _is_standards_dir(DEFAULT_STANDARDS_DIR):
        return preferred
    return candidates[0] if candidates else None


def _discover_directories(predicate) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for root in DISCOVERY_ROOTS:
        if not root.exists():
            continue
        for candidate in _iter_candidates(root):
            resolved = str(candidate.resolve())
            if resolved in seen:
                continue
            if predicate(candidate):
                found.append(resolved)
                seen.add(resolved)
    found.sort()
    return found


def _iter_candidates(root: Path):
    yield root
    for child in root.iterdir():
        if child.is_dir():
            yield child
            for nested in child.iterdir():
                if nested.is_dir():
                    yield nested


def _is_standards_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    has_docx = any(path.glob("*.docx"))
    has_xlsx = any(path.glob("*.xlsx"))
    return has_docx or has_xlsx
