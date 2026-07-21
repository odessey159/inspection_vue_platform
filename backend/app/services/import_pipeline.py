from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil

from sqlmodel import Session, select

from ..models import Project
from ..settings import DEFAULT_STANDARDS_DIR, DISCOVERY_ROOTS, PROJECTS_DIR, SECURITY_CHECK_CALIBRATION_PATH, SUPPORTED_VISION_MODELS, VISION_MODEL
from .dataset import build_dataset_summary, parse_pairs_csv, parse_pose_csv
from .extractors import export_camera_lidar_pairs, export_pose_calibration
from .provider import provider_available
from .provider_YOLO import provider_yolo_available, yolo_available
from .rosbag import is_rosbag_dir, parse_rosbag_summary
from .rtsp_recorder import DEFAULT_RTSP_RECORD_SECONDS, DEFAULT_RTSP_URL
from .rtsp_vehicles import is_point_cloud_enabled, point_cloud_settings_payload, rtsp_vehicle_payloads
from .rtsp_auto_analysis import rtsp_auto_analysis_settings_payload
from .rtsp_watchdog import rtsp_watch_settings_payload
from .runtime import compact_project_runtime
from .rules import export_rules_payload, parse_rules, sync_rules_to_db
from .scene import build_scene
from .storage import ensure_project_dirs, read_json, write_json
from .video import build_video

SAMPLE_SCENE_PATH = Path(__file__).resolve().parents[2] / "tests" / "pcd" / "scene.json"


def bootstrap_paths() -> dict[str, object]:
    bag_dirs = _discover_bag_dirs()
    standards_dirs = _discover_standards_dirs()
    preferred_standards = _preferred_standards_dir(standards_dirs)
    rtsp_watch_settings = rtsp_watch_settings_payload()
    rtsp_auto_analysis = rtsp_auto_analysis_settings_payload()
    point_cloud = point_cloud_settings_payload()
    sample_scene = str(SAMPLE_SCENE_PATH.resolve()) if SAMPLE_SCENE_PATH.is_file() else None
    return {
        "sample_bag_dir": bag_dirs[0] if bag_dirs else None,
        "sample_scene_path": sample_scene,
        "sample_pcd_path": sample_scene,
        "sample_standards_dir": preferred_standards,
        "default_rtsp_url": DEFAULT_RTSP_URL,
        "default_rtsp_record_seconds": DEFAULT_RTSP_RECORD_SECONDS,
        "rtsp_vehicles": rtsp_vehicle_payloads(),
        "detected_bag_dirs": bag_dirs,
        "detected_standards_dirs": standards_dirs,
        "provider_available": provider_available(),
        "yolo_available": yolo_available(),
        "provider_yolo_available": provider_yolo_available(),
        "default_analysis_model": VISION_MODEL,
        "supported_analysis_models": SUPPORTED_VISION_MODELS,
        "rtsp_watch_test_mode": rtsp_watch_settings["test_mode"],
        "rtsp_watch_test_max_seconds": rtsp_watch_settings["test_max_seconds"],
        "rtsp_auto_analysis_enabled": rtsp_auto_analysis["enabled"],
        "rtsp_auto_analysis_mode": rtsp_auto_analysis["mode"],
        "point_cloud_enabled": point_cloud["point_cloud_enabled"],
    }


def resolve_scene_path(path: Path) -> Path:
    path = path.resolve()
    if path.is_file() and path.name.lower() == "scene.json":
        return path
    if path.is_file() and path.suffix.lower() == ".json" and "scene" in path.stem.lower():
        return path
    if path.is_dir():
        candidate = path / "scene.json"
        if candidate.is_file():
            return candidate
        matches = sorted(path.glob("*scene*.json"))
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(f"Multiple scene JSON files found in {path}; pass a specific scene.json path")
    raise FileNotFoundError(f"Not a scene.json file or directory containing one: {path}")


def is_static_scene_import_source(path: Path) -> bool:
    try:
        resolve_scene_path(path)
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


# Backward-compatible aliases used by older tests/call sites.
def resolve_pcd_path(path: Path) -> Path:
    return resolve_scene_path(path)


def is_pcd_import_source(path: Path) -> bool:
    return is_static_scene_import_source(path)


def list_projects(session: Session) -> list[Project]:
    return list(session.exec(select(Project).order_by(Project.created_at.desc())))


def import_static_scene_project(session: Session, name: str, scene_source: Path, standards_dir: Path) -> Project:
    source_scene_path = resolve_scene_path(scene_source)
    standards_dir = standards_dir.resolve() if str(standards_dir).strip() else Path(".")
    standards_available = standards_dir.exists() and standards_dir.is_dir() and _is_standards_dir(standards_dir)

    project = Project(
        name=name,
        status="indexing",
        bag_dir=str(source_scene_path),
        standards_dir=str(standards_dir) if standards_available else "",
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
        rules = parse_rules(standards_dir, project.id) if standards_available else []
        rules_path = project_dirs["summaries"] / "rules.json"
        scene_path = project_dirs["scenes"] / "scene.json"
        dataset_summary_path = project_dirs["summaries"] / "dataset_summary.json"

        shutil.copy2(source_scene_path, scene_path)
        scene_payload = read_json(scene_path)
        if not isinstance(scene_payload, dict) or not scene_payload.get("points"):
            raise ValueError(f"Invalid scene.json payload: {source_scene_path}")

        if not standards_available:
            notes = list(scene_payload.get("notes", []))
            notes.append("Standards directory was not provided; scene-only import with empty rules.")
            scene_payload["notes"] = notes
            write_json(scene_path, scene_payload)

        write_json(
            dataset_summary_path,
            {
                "source_type": "static_scene_json",
                "scene_path": str(source_scene_path),
                "raw_point_count": int(
                    scene_payload.get("raw_point_count")
                    or scene_payload.get("scene_quality", {}).get("input_point_count", 0)
                    or len(scene_payload.get("points", []))
                ),
                "video_start_ts": 0,
                "video_end_ts": 0,
                "point_start_ts": 0,
                "point_end_ts": 0,
                "median_video_gap_ms": 0,
                "median_point_gap_ms": 0,
                "inferred_fps": 0.0,
                "time_offset_ms": 0,
            },
        )
        write_json(rules_path, export_rules_payload(rules))
        sync_rules_to_db(rules)
        for rule in rules:
            session.add(rule)

        project.status = "indexed"
        project.video_topic = None
        project.point_topic = str(source_scene_path)
        project.pose_topic = "static_scene"
        project.bag_start_ts = 0
        project.bag_end_ts = 0
        project.bag_duration_ms = 0
        project.message_count = int(
            scene_payload.get("raw_point_count")
            or scene_payload.get("scene_quality", {}).get("input_point_count", 0)
            or len(scene_payload.get("points", []))
        )
        project.rules_count = len(rules)
        project.findings_count = 0
        project.calibration_required = False
        project.time_offset_ms = 0
        project.rules_path = str(rules_path)
        project.scene_path = str(scene_path)
        project.inspection_video_path = None
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


def import_pcd_project(session: Session, name: str, pcd_path: Path, standards_dir: Path) -> Project:
    """Backward-compatible alias: static map import now reads scene.json."""
    return import_static_scene_project(session, name, pcd_path, standards_dir)


def import_project(session: Session, name: str, bag_dir: Path, standards_dir: Path) -> Project:
    bag_dir = bag_dir.resolve()
    standards_dir = standards_dir.resolve()

    if is_static_scene_import_source(bag_dir):
        return import_static_scene_project(session, name, bag_dir, standards_dir)

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
        sync_rules_to_db(rules)

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


def resolve_inspection_video_url(project: Project) -> str | None:
    if project.id is None:
        return None
    canonical = PROJECTS_DIR / str(project.id) / "artifacts" / "inspection.mp4"
    if canonical.is_file() and canonical.stat().st_size > 0:
        return f"/artifacts/{project.id}/artifacts/inspection.mp4"
    if project.inspection_video_path:
        legacy = Path(project.inspection_video_path)
        if legacy.is_file() and legacy.stat().st_size > 0:
            return f"/artifacts/{project.id}/artifacts/inspection.mp4"
    return None


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
