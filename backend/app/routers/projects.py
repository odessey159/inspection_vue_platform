from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from sqlmodel import Session, select

from ..db import get_session
from ..models import Finding, HazardRule, HazardZone, Project
from ..schemas import (
    AnalyzeRequest,
    BootstrapResponse,
    FindingResponse,
    ImageSceneRebuildResponse,
    MapImportRequest,
    MapSummary,
    PointCloudSettingsRequest,
    PointCloudSettingsResponse,
    ProjectImportRequest,
    ProjectSummary,
    RuntimeResetResponse,
    RtspPlaybackStateResponse,
    RtspRecordingsClearResponse,
    RtspVehicleResponse,
    RtspVehicleUpdateRequest,
    RtspWatchSettingsRequest,
    RtspWatchSettingsResponse,
    RuleResponse,
    SceneRebuildResponse,
    SceneResponse,
    VehicleMapAssignRequest,
    VehicleTrajectoryResponse,
    ZoneResponse,
)
from ..services.analysis import run_analysis
from ..services.analysis_summary import read_analysis_summary
from ..services.evidence import cached_frame_path
from ..services.import_pipeline import (
    bootstrap_paths,
    ensure_rtsp_vehicle_workspace,
    import_project,
    list_projects,
    project_runtime_paths,
    resolve_inspection_video_url,
)
from ..services.provider import provider_available
from ..services.provider_YOLO import provider_yolo_available, yolo_available
from ..services.runtime import reset_runtime_storage
from ..services.maps import (
    get_map,
    import_map,
    list_maps,
    load_map_for_vehicle_id,
    load_map_scene,
)
from ..services.rtsp_recorder import (
    align_scene_timestamps_to_video,
    build_rtsp_playback_state,
    clear_rtsp_recordings,
    find_latest_completed_recording_for_storage_key,
    import_rtsp_project,
    is_rtsp_project,
    resolve_project_rtsp_url,
    resolve_project_vehicle_id,
    summarize_rtsp_playback_fields,
)
from ..services.rtsp_vehicles import (
    get_vehicle_by_id,
    is_point_cloud_enabled,
    point_cloud_settings_payload,
    set_point_cloud_enabled,
    update_vehicle_map_id,
    update_vehicle_rtsp_url,
)
from ..services.rtsp_watchdog import restart_watch_for_vehicle, rtsp_watch_settings_payload, set_rtsp_watch_test_mode
from ..services.rtsp_yolo_monitor import start_rtsp_yolo_monitor, stop_rtsp_yolo_monitor
from ..services.rtsp_live import MJPEG_BOUNDARY, iter_mjpeg_multipart_frames
from ..services.vehicle_trajectory import load_vehicle_trajectory
from ..services.scene_rebuild import rebuild_project_scene
from ..services.sfm_reconstruction import rebuild_project_sfm_scene, sfm_scene_exists, sfm_scene_path
from ..services.storage import read_json, resolve_project_path


router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/bootstrap", response_model=BootstrapResponse)
def get_bootstrap() -> BootstrapResponse:
    return BootstrapResponse(**bootstrap_paths())


@router.delete("/runtime", response_model=RuntimeResetResponse)
def delete_runtime() -> RuntimeResetResponse:
    return RuntimeResetResponse(**reset_runtime_storage())


@router.delete("/rtsp-recordings", response_model=RtspRecordingsClearResponse)
def delete_rtsp_recordings() -> RtspRecordingsClearResponse:
    return RtspRecordingsClearResponse(**clear_rtsp_recordings())


@router.get("/rtsp-watch-settings", response_model=RtspWatchSettingsResponse)
def get_rtsp_watch_settings() -> RtspWatchSettingsResponse:
    return RtspWatchSettingsResponse(**rtsp_watch_settings_payload())


@router.patch("/rtsp-watch-settings", response_model=RtspWatchSettingsResponse)
def patch_rtsp_watch_settings(payload: RtspWatchSettingsRequest) -> RtspWatchSettingsResponse:
    set_rtsp_watch_test_mode(payload.test_mode)
    return RtspWatchSettingsResponse(**rtsp_watch_settings_payload())


@router.get("/point-cloud-settings", response_model=PointCloudSettingsResponse)
def get_point_cloud_settings() -> PointCloudSettingsResponse:
    return PointCloudSettingsResponse(**point_cloud_settings_payload())


@router.patch("/point-cloud-settings", response_model=PointCloudSettingsResponse)
def patch_point_cloud_settings(payload: PointCloudSettingsRequest) -> PointCloudSettingsResponse:
    set_point_cloud_enabled(payload.point_cloud_enabled)
    return PointCloudSettingsResponse(**point_cloud_settings_payload())


@router.get("/rtsp-playback-state", response_model=RtspPlaybackStateResponse)
def get_rtsp_playback_state(
    rtsp_url: str = Query(..., min_length=1),
    project_id: int | None = Query(default=None),
) -> RtspPlaybackStateResponse:
    try:
        payload = build_rtsp_playback_state(rtsp_url, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if bool(payload.get("stream_online")):
        start_rtsp_yolo_monitor(str(payload["storage_key"]), str(payload["rtsp_url"]))
    else:
        stop_rtsp_yolo_monitor(str(payload["storage_key"]))
    return RtspPlaybackStateResponse(**payload)


@router.get("/rtsp-live")
def stream_rtsp_live(rtsp_url: str = Query(..., min_length=1)) -> StreamingResponse:
    try:
        frame_iter = iter_mjpeg_multipart_frames(rtsp_url.strip())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StreamingResponse(
        iterate_in_threadpool(frame_iter),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/rtsp-recordings/{storage_key}/latest")
def get_latest_rtsp_recording(storage_key: str) -> FileResponse:
    if not storage_key or storage_key in {".", ".."} or "/" in storage_key or "\\" in storage_key:
        raise HTTPException(status_code=400, detail="Invalid storage key")
    recording_path = find_latest_completed_recording_for_storage_key(storage_key)
    if recording_path is None:
        raise HTTPException(status_code=404, detail="RTSP recording not found")
    return FileResponse(recording_path, media_type="video/mp4", filename=recording_path.name)


@router.patch("/rtsp-vehicles/{vehicle_id}", response_model=RtspVehicleResponse)
def patch_rtsp_vehicle(
    vehicle_id: str,
    payload: RtspVehicleUpdateRequest,
    session: Session = Depends(get_session),
) -> RtspVehicleResponse:
    """Update a vehicle RTSP URL from the frontend; persists and takes effect immediately."""
    try:
        vehicle = update_vehicle_rtsp_url(vehicle_id, payload.rtsp_url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Failed to persist vehicle RTSP URL: {exc}") from exc

    _sync_vehicle_workspace_url(session, vehicle.id, vehicle.rtsp_url)
    restart_watch_for_vehicle(vehicle.id)
    return RtspVehicleResponse(id=vehicle.id, name=vehicle.name, rtsp_url=vehicle.rtsp_url, map_id=vehicle.map_id)


@router.patch("/rtsp-vehicles/{vehicle_id}/map", response_model=RtspVehicleResponse)
def patch_rtsp_vehicle_map(vehicle_id: str, payload: VehicleMapAssignRequest) -> RtspVehicleResponse:
    """Bind or clear a catalog map_id on a vehicle. Does not copy map files."""
    try:
        vehicle = update_vehicle_map_id(vehicle_id, payload.map_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Failed to persist vehicle map index: {exc}") from exc
    return RtspVehicleResponse(id=vehicle.id, name=vehicle.name, rtsp_url=vehicle.rtsp_url, map_id=vehicle.map_id)


@router.post("/rtsp-vehicles/{vehicle_id}/workspace", response_model=ProjectSummary)
def post_rtsp_vehicle_workspace(vehicle_id: str, session: Session = Depends(get_session)) -> ProjectSummary:
    """Open or create the vehicle workspace without importing a recording."""
    try:
        project = ensure_rtsp_vehicle_workspace(session, vehicle_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_summary(project)


@router.get("/rtsp-vehicles/{vehicle_id}/scene", response_model=SceneResponse)
def get_vehicle_scene(vehicle_id: str) -> SceneResponse:
    """Load the catalog map referenced by this vehicle's map_id, plus its RTSP trajectory."""
    cleaned = vehicle_id.strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid vehicle id")
    if not is_point_cloud_enabled():
        raise HTTPException(status_code=404, detail="Point cloud map is disabled")
    vehicle = get_vehicle_by_id(cleaned)
    if vehicle is None:
        raise HTTPException(status_code=404, detail=f"Unknown RTSP vehicle id: {cleaned}")
    if not vehicle.map_id:
        raise HTTPException(
            status_code=404,
            detail=f"Vehicle '{vehicle.id}' has no map_id",
        )

    payload = load_map_for_vehicle_id(cleaned)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Map '{vehicle.map_id}' has no processed scene.json")
    notes = list(payload.get("notes", [])) if isinstance(payload.get("notes"), list) else []
    preview_note = f"已按索引加载地图 {vehicle.map_id}（{vehicle.name}）"
    if preview_note not in notes:
        notes = [preview_note, *notes]
    payload = {**payload, "notes": notes}
    quality = payload.get("scene_quality")
    if not isinstance(quality, dict):
        quality = {}
    payload["scene_quality"] = {
        **quality,
        "vehicle_id": vehicle.id,
        "vehicle_name": vehicle.name,
        "map_id": vehicle.map_id,
    }
    return _scene_response_from_payload(payload, project_id=0)


@router.get("/rtsp-vehicles/{vehicle_id}/trajectory", response_model=VehicleTrajectoryResponse)
def get_vehicle_trajectory(vehicle_id: str) -> VehicleTrajectoryResponse:
    cleaned = vehicle_id.strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid vehicle id")
    vehicle = get_vehicle_by_id(cleaned)
    if vehicle is None:
        raise HTTPException(status_code=404, detail=f"Unknown RTSP vehicle id: {cleaned}")
    record = load_vehicle_trajectory(cleaned)
    return VehicleTrajectoryResponse(**record.payload())


@router.get("/maps", response_model=list[MapSummary])
def get_maps() -> list[MapSummary]:
    return [MapSummary(**record.payload()) for record in list_maps()]


@router.get("/maps/{map_id}", response_model=MapSummary)
def get_map_summary(map_id: str) -> MapSummary:
    record = get_map(map_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown map id: {map_id}")
    return MapSummary(**record.payload())


@router.get("/maps/{map_id}/scene", response_model=SceneResponse)
def get_map_scene(map_id: str) -> SceneResponse:
    if not is_point_cloud_enabled():
        raise HTTPException(status_code=404, detail="Point cloud map is disabled")
    if get_map(map_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown map id: {map_id}")
    try:
        payload = load_map_scene(map_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid catalog map: {exc}") from exc
    return _scene_response_from_payload(payload, project_id=0)


@router.post("/maps/import", response_model=MapSummary)
def post_import_map(payload: MapImportRequest) -> MapSummary:
    source = Path(payload.path.strip())
    try:
        record = import_map(source, name=payload.name, map_id=payload.map_id)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    assign_id = (payload.assign_vehicle_id or "").strip()
    if assign_id:
        try:
            update_vehicle_map_id(assign_id, record.id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MapSummary(**record.payload())


@router.get("/projects", response_model=list[ProjectSummary])
def get_projects(session: Session = Depends(get_session)) -> list[ProjectSummary]:
    return [_project_summary(project) for project in list_projects(session)]


@router.post("/projects/import", response_model=ProjectSummary)
def post_import_project(payload: ProjectImportRequest, session: Session = Depends(get_session)) -> ProjectSummary:
    try:
        source = payload.bag_dir.strip()
        if source.lower().startswith("rtsp://"):
            project = import_rtsp_project(
                session=session,
                name=payload.name,
                rtsp_url=source,
                standards_dir=Path(payload.standards_dir),
                vehicle_id=payload.vehicle_id,
                duration_sec=payload.rtsp_duration_sec or 60,
                rtsp_transport=payload.rtsp_transport or "tcp",
            )
        else:
            project = import_project(
                session=session,
                name=payload.name,
                bag_dir=Path(source),
                standards_dir=Path(payload.standards_dir),
                vehicle_id=payload.vehicle_id,
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _project_summary(project)


@router.get("/projects/{project_id}", response_model=ProjectSummary)
def get_project(project_id: int, session: Session = Depends(get_session)) -> ProjectSummary:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_summary(project)


@router.post("/projects/{project_id}/analyze", response_model=list[FindingResponse])
def analyze_project(project_id: int, payload: AnalyzeRequest, session: Session = Depends(get_session)) -> list[FindingResponse]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        findings = run_analysis(
            session,
            project,
            mode=payload.mode,
            model=payload.model,
            record_fresh_rtsp=payload.record_fresh_rtsp,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_finding_response(session, finding) for finding in findings]


@router.get("/projects/{project_id}/rules", response_model=list[RuleResponse])
def get_rules(project_id: int, session: Session = Depends(get_session)) -> list[RuleResponse]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    rules = list(session.exec(select(HazardRule).where(HazardRule.project_id == project_id).order_by(HazardRule.rule_id)))
    return [_rule_response(rule) for rule in rules]


@router.get("/projects/{project_id}/findings", response_model=list[FindingResponse])
def get_findings(project_id: int, session: Session = Depends(get_session)) -> list[FindingResponse]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    findings = list(session.exec(select(Finding).where(Finding.project_id == project_id).order_by(Finding.time_start_ms)))
    return [_finding_response(session, finding) for finding in findings]


@router.get("/projects/{project_id}/scene", response_model=SceneResponse)
def get_scene(
    project_id: int,
    source: Literal["lidar", "sfm"] = Query(default="lidar"),
    session: Session = Depends(get_session),
) -> SceneResponse:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project scene not found")

    if source == "sfm":
        scene_path = _scene_path_for_source(project, source)
        if scene_path is None or not scene_path.exists():
            raise HTTPException(status_code=404, detail="Project scene not found")
        payload = read_json(scene_path)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid SFM scene payload")
    else:
        payload = load_map_for_vehicle_id(resolve_project_vehicle_id(project))
        if payload is None:
            raise HTTPException(status_code=404, detail="No point-cloud map bound to this vehicle")
        quality = payload.get("scene_quality")
        skip_align = isinstance(quality, dict) and quality.get("trajectory_source") == "rtsp_sei"
        start_ts = int(project.bag_start_ts or 0)
        end_ts = int(project.bag_end_ts or 0)
        if not skip_align and start_ts and end_ts and end_ts > start_ts:
            payload = align_scene_timestamps_to_video(payload, start_ts, end_ts)

    zones = list(session.exec(select(HazardZone).where(HazardZone.project_id == project_id)))
    return _scene_response_from_payload(
        payload,
        project_id=project_id,
        source=source,
        hazard_zones=[_zone_response(zone) for zone in zones],
    )


@router.post("/projects/{project_id}/rebuild-scene", response_model=SceneRebuildResponse)
def post_rebuild_scene(project_id: int, session: Session = Depends(get_session)) -> SceneRebuildResponse:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        result = rebuild_project_scene(session, project)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = result["scene"]
    refreshed_project = result["project"]
    scene_url = f"/api/projects/{project_id}/scene" if refreshed_project.scene_path else None
    return SceneRebuildResponse(
        project_id=project_id,
        status="rebuilt",
        scene_url=scene_url,
        colorized=bool(payload.get("colorized", False)),
        color_source=str(payload.get("color_source", "structure_enhanced")),
        raw_point_count=int(payload.get("raw_point_count", len(payload.get("points", [])))),
        render_point_count=int(payload.get("render_point_count", len(payload.get("render_points", [])))),
        updated_at=refreshed_project.updated_at,
        notes=list(payload.get("notes", [])),
    )


@router.post("/projects/{project_id}/rebuild-scene-from-images", response_model=ImageSceneRebuildResponse)
def post_rebuild_scene_from_images(project_id: int, session: Session = Depends(get_session)) -> ImageSceneRebuildResponse:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        result = rebuild_project_sfm_scene(session, project)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ImageSceneRebuildResponse(
        project_id=project_id,
        status="rebuilt",
        scene_source="sfm",
        selected_image_count=int(result.get("selected_image_count", 0)),
        registered_image_count=int(result.get("registered_image_count", 0)),
        dense_point_count=int(result.get("dense_point_count", 0)),
        aligned=True,
        alignment_rmse_m=_safe_float(result.get("alignment_rmse_m")),
        notes=[str(item) for item in result.get("notes", [])],
    )


@router.get("/projects/{project_id}/video-frames/{timestamp_ms}")
def get_video_frame(project_id: int, timestamp_ms: int, session: Session = Depends(get_session)) -> FileResponse:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    frame_path = cached_frame_path(project, timestamp_ms)
    if frame_path is None or not frame_path.exists():
        raise HTTPException(status_code=404, detail="Evidence frame cache not found. Run analysis to generate preview frames.")
    return FileResponse(frame_path)


@router.get("/projects/{project_id}/rtsp-live")
def stream_project_rtsp_live(project_id: int, session: Session = Depends(get_session)) -> StreamingResponse:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not is_rtsp_project(project):
        raise HTTPException(status_code=400, detail="Project is not RTSP-sourced")
    try:
        frame_iter = iter_mjpeg_multipart_frames(resolve_project_rtsp_url(project))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StreamingResponse(
        iterate_in_threadpool(frame_iter),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _project_summary(project: Project) -> ProjectSummary:
    runtime_paths = project_runtime_paths(project)
    dataset_summary = read_json(runtime_paths["dataset_summary"]) if runtime_paths["dataset_summary"].exists() else {}
    analysis_summary = read_analysis_summary(project)
    scene_url = None
    if project.id is not None:
        vehicle = get_vehicle_by_id((project.vehicle_id or "").strip()) if project.vehicle_id else None
        if vehicle and vehicle.map_id:
            scene_url = f"/api/projects/{project.id}/scene"
    inspection_video_url = resolve_inspection_video_url(project)
    rtsp_live_url = None
    rtsp_recording_active = False
    rtsp_stream_online = False
    rtsp_recorded_video_url = None
    live_source = resolve_project_rtsp_url(project) if is_rtsp_project(project) else ""
    if is_rtsp_project(project) and live_source:
        playback_state = summarize_rtsp_playback_fields(live_source, project_id=project.id)
        rtsp_live_url = str(playback_state["live_url"])
        rtsp_recording_active = bool(playback_state["recording_active"])
        rtsp_stream_online = bool(playback_state.get("stream_online"))
        recorded_video_url = playback_state.get("recorded_video_url")
        rtsp_recorded_video_url = str(recorded_video_url) if recorded_video_url else None
    analysis_mode = analysis_summary.get("analysis_mode")
    available_scene_sources = _available_scene_sources(project)
    return ProjectSummary(
        id=project.id or 0,
        name=project.name,
        status=project.status,
        vehicle_id=project.vehicle_id,
        bag_dir=live_source or project.bag_dir,
        standards_dir=project.standards_dir,
        video_topic=project.video_topic,
        point_topic=project.point_topic,
        pose_topic=project.pose_topic,
        video_start_ts=_safe_int(dataset_summary.get("video_start_ts")),
        video_end_ts=_safe_int(dataset_summary.get("video_end_ts")),
        point_start_ts=_safe_int(dataset_summary.get("point_start_ts")),
        point_end_ts=_safe_int(dataset_summary.get("point_end_ts")),
        median_video_gap_ms=_safe_int(dataset_summary.get("median_video_gap_ms")),
        median_point_gap_ms=_safe_int(dataset_summary.get("median_point_gap_ms")),
        inferred_fps=_safe_float(dataset_summary.get("inferred_fps")),
        bag_start_ts=project.bag_start_ts,
        bag_end_ts=project.bag_end_ts,
        bag_duration_ms=project.bag_duration_ms,
        message_count=project.message_count,
        rules_count=project.rules_count,
        findings_count=project.findings_count,
        calibration_required=project.calibration_required,
        time_offset_ms=project.time_offset_ms,
        scene_url=scene_url,
        inspection_video_url=inspection_video_url,
        rtsp_live_url=rtsp_live_url,
        rtsp_recording_active=rtsp_recording_active,
        rtsp_stream_online=rtsp_stream_online,
        rtsp_recorded_video_url=rtsp_recorded_video_url,
        available_scene_sources=available_scene_sources,
        default_scene_source="lidar",
        sfm_available="sfm" in available_scene_sources,
        provider_available=provider_available(),
        yolo_available=yolo_available(),
        provider_yolo_available=provider_yolo_available(),
        analysis_mode=(
            analysis_mode
            if analysis_mode in {"demo", "provider", "provider_yolo", "provider_yolo_monitor"}
            else None
        ),
        analysis_provider=_safe_str(analysis_summary.get("analysis_provider")),
        analysis_model=_safe_str(analysis_summary.get("analysis_model")),
        analysis_notes=_safe_list_of_str(analysis_summary.get("notes")),
        analysis_diagnostics=_safe_list_of_str(analysis_summary.get("diagnostics")),
        analysis_updated_at=analysis_summary.get("updated_at"),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _scene_path_for_source(project: Project, source: Literal["lidar", "sfm"]) -> Path | None:
    if source == "sfm":
        path = sfm_scene_path(project)
        return path if path.exists() else None
    if not project.artifacts_dir and not project.scene_path:
        return None
    path = resolve_project_path(project.artifacts_dir or "", project.scene_path, "scenes/scene.json")
    return path if path.exists() else None


def _scene_response_from_payload(
    payload: dict,
    *,
    project_id: int,
    source: Literal["lidar", "sfm"] = "lidar",
    hazard_zones: list[ZoneResponse] | None = None,
) -> SceneResponse:
    return SceneResponse(
        project_id=project_id,
        scene_source=payload.get("scene_source", source),
        reconstruction_method=payload.get(
            "reconstruction_method",
            "lidar_projection" if source == "lidar" else "colmap_sfm_mvs",
        ),
        points=payload.get("points", []),
        full_points=payload.get("full_points", payload.get("points", [])),
        roof_removed_points=payload.get("roof_removed_points", payload.get("points", [])),
        floor_removed_points=payload.get("floor_removed_points", payload.get("points", [])),
        structure_points=payload.get("structure_points", payload.get("render_points", [])),
        render_points=payload.get("render_points", []),
        default_point_mode=payload.get(
            "default_point_mode",
            "roof_removed" if payload.get("roof_removed_points") else "full",
        ),
        trajectory=payload.get("trajectory", []),
        trajectory_timestamps=payload.get("trajectory_timestamps", []),
        trajectory_orientations=payload.get("trajectory_orientations", []),
        bounds=payload.get("bounds", {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}),
        full_bounds=payload.get("full_bounds", payload.get("bounds", {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]})),
        roof_removed_bounds=payload.get(
            "roof_removed_bounds",
            payload.get("bounds", {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}),
        ),
        source_frame_count=payload.get("source_frame_count", 0),
        coordinate_frame=payload.get("coordinate_frame", "sensor_local"),
        source_type=payload.get("source_type", "unknown"),
        raw_point_count=payload.get("raw_point_count", len(payload.get("points", []))),
        render_point_count=payload.get("render_point_count", len(payload.get("render_points", []))),
        structure_point_count=int(payload.get("structure_point_count", len(payload.get("structure_points", [])))),
        colorized=bool(payload.get("colorized", False)),
        color_source=str(payload.get("color_source", "structure_enhanced")),
        cut_height_default=float(payload.get("cut_height_default", 0.0)),
        floor_cut_default=float(payload.get("floor_cut_default", 0.0)),
        scene_quality=payload.get("scene_quality", {}),
        selected_image_count=int(payload.get("selected_image_count", 0)),
        registered_image_count=int(payload.get("registered_image_count", 0)),
        alignment_status=str(payload.get("alignment_status", "not_applicable")),
        alignment_rmse_m=_safe_float(payload.get("alignment_rmse_m")),
        notes=payload.get("notes", []),
        hazard_zones=hazard_zones or [],
    )


def _available_scene_sources(project: Project) -> list[Literal["lidar", "sfm"]]:
    sources: list[Literal["lidar", "sfm"]] = []
    vehicle = get_vehicle_by_id((project.vehicle_id or "").strip()) if project.vehicle_id else None
    if vehicle and vehicle.map_id:
        sources.append("lidar")
    if sfm_scene_exists(project):
        sources.append("sfm")
    if not sources:
        sources.append("lidar")
    return sources



def _rule_response(rule: HazardRule) -> RuleResponse:
    return RuleResponse(
        rule_id=rule.rule_id,
        domain=rule.domain,
        category=rule.category,
        object_name=rule.object_name,
        check_item=rule.check_item,
        checker_scope=rule.checker_scope,
        hazard_desc=rule.hazard_desc,
        legal_basis=rule.legal_basis,
        evidence_objects=json.loads(rule.evidence_objects_json),
        severity=rule.severity,
        visual_detectable=rule.visual_detectable,
        source=rule.source,
    )



def _zone_response(zone: HazardZone) -> ZoneResponse:
    return ZoneResponse(
        id=zone.id or 0,
        finding_id=zone.finding_id,
        center=[zone.center_x, zone.center_y, zone.center_z],
        radius_m=zone.radius_m,
        heading=zone.heading,
        related_pose_ts=zone.related_pose_ts,
    )



def _finding_response(session: Session, finding: Finding) -> FindingResponse:
    rule = session.exec(
        select(HazardRule).where(HazardRule.project_id == finding.project_id).where(HazardRule.rule_id == finding.rule_id)
    ).first()
    zone = session.exec(select(HazardZone).where(HazardZone.finding_id == (finding.id or 0))).first()
    return FindingResponse(
        id=finding.id or 0,
        finding_uid=finding.finding_uid,
        rule_id=finding.rule_id,
        title=finding.title,
        time_start_ms=finding.time_start_ms,
        time_end_ms=finding.time_end_ms,
        evidence_frame_ts=json.loads(finding.evidence_frame_ts_json),
        description=finding.description,
        confidence=finding.confidence,
        needs_review=finding.needs_review,
        review_status=finding.review_status,
        reviewer_notes=finding.reviewer_notes,
        severity=finding.severity,
        analysis_mode=finding.analysis_mode,
        legal_basis=rule.legal_basis if rule else "",
        hazard_desc=rule.hazard_desc if rule else "",
        category=rule.category if rule else "",
        checker_scope=rule.checker_scope if rule else "",
        visual_detectable=rule.visual_detectable if rule else False,
        zone=_zone_response(zone) if zone else None,
    )



def _sync_vehicle_workspace_url(session: Session, vehicle_id: str, rtsp_url: str) -> None:
    for project in session.exec(select(Project).where(Project.vehicle_id == vehicle_id)).all():
        if not is_rtsp_project(project):
            continue
        project.bag_dir = rtsp_url
        session.add(project)
    session.commit()


def _safe_int(value) -> int | None:
    if value is None:
        return None
    return int(value)



def _safe_float(value) -> float | None:
    if value is None:
        return None
    return float(value)



def _safe_str(value) -> str | None:
    if value is None:
        return None
    return str(value)



def _safe_list_of_str(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
