"""Project import orchestration for rosbag directories and RTSP vehicle streams."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, delete, select

from ..models import Finding, HazardRule, HazardZone, Project
from ..settings import DEFAULT_STANDARDS_DIR, DISCOVERY_ROOTS, PROJECTS_DIR, SUPPORTED_VISION_MODELS, VISION_MODEL
from .dataset import build_dataset_summary, parse_pairs_csv
from .extractors import export_camera_lidar_pairs, export_pose_calibration
from .provider import provider_available
from .provider_YOLO import provider_yolo_available, yolo_available
from .rosbag import is_rosbag_dir, parse_rosbag_summary
from .rtsp_recorder import DEFAULT_RTSP_RECORD_SECONDS, DEFAULT_RTSP_URL
from .maps import list_maps
from .rtsp_vehicles import is_point_cloud_enabled, point_cloud_settings_payload, rtsp_vehicle_payloads
from .rtsp_auto_analysis import rtsp_auto_analysis_settings_payload
from .rtsp_watchdog import rtsp_watch_settings_payload
from .runtime import compact_project_runtime
from .rules import export_rules_payload, parse_rules, sync_rules_to_db
from .storage import (
    OFFLINE_WORKSPACE_ID,
    ensure_project_dirs_for,
    project_workspace_id,
    sanitize_workspace_key,
    to_project_relative_path,
    workspace_root,
    write_json,
)
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
        "maps": [record.payload() for record in list_maps()],
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


def backfill_vehicle_workspaces() -> None:
    """Assign vehicle_id and copy leftover ``projects/<id>`` dirs into ``robots/<vehicle>``."""
    from ..db import engine

    with Session(engine) as session:
        rows = list(session.exec(select(Project).order_by(Project.updated_at.desc())))
        for project in rows:
            key = (project.vehicle_id or "").strip()
            if not key:
                topic = (project.point_topic or "").strip()
                if topic and not topic.startswith("/"):
                    key = sanitize_workspace_key(topic)
                elif (project.bag_dir or "").strip().lower().startswith("rtsp://"):
                    from .rtsp_recorder import resolve_storage_key_for_rtsp_url

                    try:
                        key = sanitize_workspace_key(resolve_storage_key_for_rtsp_url(project.bag_dir.strip()))
                    except ValueError:
                        key = OFFLINE_WORKSPACE_ID
                else:
                    key = OFFLINE_WORKSPACE_ID
                project.vehicle_id = key
            dirs = ensure_project_dirs_for(project)
            project.artifacts_dir = str(dirs["root"])
            session.add(project)
        session.commit()


def ensure_configured_vehicle_workspaces() -> None:
    """Create a lightweight RTSP workspace row for every configured vehicle."""
    from ..db import engine
    from .rtsp_vehicles import load_rtsp_vehicles

    with Session(engine) as session:
        for vehicle in load_rtsp_vehicles():
            ensure_rtsp_vehicle_workspace(session, vehicle.id)


def ensure_rtsp_vehicle_workspace(
    session: Session,
    vehicle_id: str,
    *,
    name: str | None = None,
    standards_dir: str | None = None,
) -> Project:
    """Create or refresh a vehicle workspace without starting a recording or clearing findings."""
    from .rtsp_vehicles import get_vehicle_by_id

    vehicle = get_vehicle_by_id(vehicle_id)
    if vehicle is None:
        raise KeyError(f"Unknown RTSP vehicle id: {(vehicle_id or '').strip() or vehicle_id}")

    now = datetime.now(timezone.utc)
    project = get_project_by_vehicle_id(session, vehicle.id)
    resolved_standards = (standards_dir or "").strip()
    if not resolved_standards and project is not None:
        resolved_standards = (project.standards_dir or "").strip()
    if not resolved_standards and DEFAULT_STANDARDS_DIR.is_dir():
        resolved_standards = str(DEFAULT_STANDARDS_DIR)

    if project is None or project.id is None:
        project = Project(
            name=(name or "").strip() or vehicle.name,
            status="watching",
            vehicle_id=vehicle.id,
            bag_dir=vehicle.rtsp_url,
            standards_dir=resolved_standards,
            artifacts_dir="",
            point_topic=vehicle.id,
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        if project.id is None:
            raise RuntimeError("Project creation failed")
        dirs = ensure_project_dirs_for(project)
        project.artifacts_dir = str(dirs["root"])
        session.add(project)
        session.commit()
        session.refresh(project)
        _seed_rtsp_rules_if_empty(session, project)
        return project

    project.bag_dir = vehicle.rtsp_url
    project.vehicle_id = vehicle.id
    if (name or "").strip():
        project.name = name.strip()
    if (standards_dir or "").strip():
        project.standards_dir = standards_dir.strip()
    if not project.artifacts_dir:
        dirs = ensure_project_dirs_for(project)
        project.artifacts_dir = str(dirs["root"])
    project.updated_at = now
    session.add(project)
    session.commit()
    session.refresh(project)
    _seed_rtsp_rules_if_empty(session, project)
    return project


def _seed_rtsp_rules_if_empty(session: Session, project: Project) -> None:
    if project.id is None or project.rules_count:
        return
    standards = Path(project.standards_dir) if project.standards_dir else None
    if standards is None or not _is_standards_dir(standards):
        return
    try:
        rules = parse_rules(standards, project.id)
    except Exception:
        return
    if not rules:
        return
    project_dirs = ensure_project_dirs_for(project)
    rules_path = project_dirs["summaries"] / "rules.json"
    write_json(rules_path, export_rules_payload(rules))
    sync_rules_to_db(rules)
    for rule in rules:
        session.add(rule)
    project.rules_count = len(rules)
    project.rules_path = str(rules_path)
    session.add(project)
    session.commit()
    session.refresh(project)


def list_projects(session: Session) -> list[Project]:
    rows = list(session.exec(select(Project).order_by(Project.updated_at.desc())))
    unique: list[Project] = []
    seen: set[str] = set()
    for project in rows:
        key = (project.vehicle_id or "").strip() or f"id:{project.id}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(project)
    return unique


def get_project_by_vehicle_id(session: Session, vehicle_id: str) -> Project | None:
    key = sanitize_workspace_key(vehicle_id)
    return session.exec(
        select(Project).where(Project.vehicle_id == key).order_by(Project.updated_at.desc())
    ).first()


def _clear_project_records(session: Session, project_id: int) -> None:
    """Stage replacement of project-owned rows in the caller's transaction."""
    session.exec(delete(HazardZone).where(HazardZone.project_id == project_id))
    session.exec(delete(Finding).where(Finding.project_id == project_id))
    session.exec(delete(HazardRule).where(HazardRule.project_id == project_id))


def prepare_vehicle_workspace(
    session: Session,
    *,
    name: str,
    bag_dir: str,
    standards_dir: str,
    vehicle_id: str | None,
    rtsp_vehicle: bool = False,
) -> Project:
    """Create or reuse the single workspace row for a vehicle (or the offline workspace)."""
    key = sanitize_workspace_key(vehicle_id)
    now = datetime.now(timezone.utc)
    project = get_project_by_vehicle_id(session, key)
    if project is not None and project.id is not None:
        project.name = name
        project.status = "indexing"
        project.vehicle_id = key
        project.bag_dir = bag_dir
        project.standards_dir = standards_dir
        if rtsp_vehicle:
            project.point_topic = key
        project.updated_at = now
        session.add(project)
        session.commit()
        session.refresh(project)
    else:
        project = Project(
            name=name,
            status="indexing",
            vehicle_id=key,
            bag_dir=bag_dir,
            standards_dir=standards_dir,
            artifacts_dir="",
            point_topic=key if rtsp_vehicle else None,
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        if project.id is None:
            raise RuntimeError("Project creation failed")

    project_dirs = ensure_project_dirs_for(project)
    project.artifacts_dir = str(project_dirs["root"])
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def import_static_scene_project(
    session: Session,
    name: str,
    scene_source: Path,
    standards_dir: Path,
    vehicle_id: str | None = None,
) -> Project:
    del session, name, scene_source, standards_dir, vehicle_id
    raise ValueError(
        "scene.json / PCD 是独立点云地图，请使用 POST /api/maps/import 导入，再在小车上绑定 map_id。"
    )


def import_pcd_project(session: Session, name: str, pcd_path: Path, standards_dir: Path) -> Project:
    """Backward-compatible alias: static map import now reads scene.json."""
    return import_static_scene_project(session, name, pcd_path, standards_dir)


def import_project(
    session: Session,
    name: str,
    bag_dir: Path,
    standards_dir: Path,
    vehicle_id: str | None = None,
) -> Project:
    bag_dir = bag_dir.resolve()
    standards_dir = standards_dir.resolve()

    if is_static_scene_import_source(bag_dir) and not is_rosbag_dir(bag_dir):
        raise ValueError(
            "scene.json / PCD 是独立点云地图，请使用 POST /api/maps/import 导入，再在小车上绑定 map_id。"
        )

    if not is_rosbag_dir(bag_dir):
        raise FileNotFoundError(f"Not a valid rosbag directory: {bag_dir}")
    if not standards_dir.exists() or not standards_dir.is_dir():
        raise FileNotFoundError(f"Standards directory not found: {standards_dir}")

    project = prepare_vehicle_workspace(
        session,
        name=name,
        bag_dir=str(bag_dir),
        standards_dir=str(standards_dir),
        vehicle_id=vehicle_id,
        rtsp_vehicle=False,
    )
    project_dirs = ensure_project_dirs_for(project)

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
        rules = parse_rules(standards_dir, project.id)

        rosbag_summary_path = project_dirs["summaries"] / "rosbag_summary.json"
        dataset_summary_path = project_dirs["summaries"] / "dataset_summary.json"
        rules_path = project_dirs["summaries"] / "rules.json"
        video_manifest_path = project_dirs["manifests"] / "video_manifest.json"
        video_output_path = project_dirs["artifacts"] / "inspection.mp4"

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
        compact_project_runtime(project_dirs["root"])

        _clear_project_records(session, project.id)
        for rule in rules:
            session.add(rule)

        project.status = "indexed"
        project.video_topic = video_topic
        project.point_topic = point_topic
        project.pose_topic = None
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
        project.scene_path = None
        project.inspection_video_path = to_project_relative_path(project_dirs["root"], video_output_path)
        project.updated_at = datetime.now(timezone.utc)

        session.add(project)
        session.commit()
        session.refresh(project)
        return project
    except Exception:
        session.rollback()
        if project.id is not None:
            project = session.get(Project, project.id) or project
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
    key = project_workspace_id(project)
    canonical = workspace_root(key) / "artifacts" / "inspection.mp4"
    if canonical.is_file() and canonical.stat().st_size > 0:
        return f"/artifacts/{key}/artifacts/inspection.mp4"
    if project.inspection_video_path:
        resolved = Path(project.artifacts_dir) / project.inspection_video_path if project.artifacts_dir else Path(project.inspection_video_path)
        if not resolved.is_file() and Path(project.inspection_video_path).is_file():
            resolved = Path(project.inspection_video_path)
        if resolved.is_file() and resolved.stat().st_size > 0:
            dirs = ensure_project_dirs_for(project)
            migrated = dirs["artifacts"] / "inspection.mp4"
            if migrated.is_file() and migrated.stat().st_size > 0:
                return f"/artifacts/{key}/artifacts/inspection.mp4"
    if project.id is not None:
        legacy = PROJECTS_DIR / str(project.id) / "artifacts" / "inspection.mp4"
        if legacy.is_file() and legacy.stat().st_size > 0:
            dirs = ensure_project_dirs_for(project)
            migrated = dirs["artifacts"] / "inspection.mp4"
            if migrated.is_file() and migrated.stat().st_size > 0:
                return f"/artifacts/{key}/artifacts/inspection.mp4"
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
