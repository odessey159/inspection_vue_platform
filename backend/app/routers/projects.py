from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..db import get_session
from ..models import Finding, HazardRule, HazardZone, Project
from ..schemas import (
    AnalyzeRequest,
    BootstrapResponse,
    FindingResponse,
    ImageSceneRebuildResponse,
    ProjectImportRequest,
    ProjectSummary,
    RuntimeResetResponse,
    RuleResponse,
    SceneRebuildResponse,
    SceneResponse,
    ZoneResponse,
)
from ..services.analysis import run_analysis
from ..services.analysis_summary import read_analysis_summary
from ..services.evidence import cached_frame_path
from ..services.import_pipeline import bootstrap_paths, import_project, list_projects, project_runtime_paths
from ..services.provider import provider_available
from ..services.runtime import reset_runtime_storage
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


@router.get("/projects", response_model=list[ProjectSummary])
def get_projects(session: Session = Depends(get_session)) -> list[ProjectSummary]:
    return [_project_summary(project) for project in list_projects(session)]


@router.post("/projects/import", response_model=ProjectSummary)
def post_import_project(payload: ProjectImportRequest, session: Session = Depends(get_session)) -> ProjectSummary:
    try:
        project = import_project(
            session=session,
            name=payload.name,
            bag_dir=Path(payload.bag_dir),
            standards_dir=Path(payload.standards_dir),
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
        findings = run_analysis(session, project, mode=payload.mode, model=payload.model)
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

    scene_path = _scene_path_for_source(project, source)
    if scene_path is None or not scene_path.exists():
        raise HTTPException(status_code=404, detail="Project scene not found")
    payload = read_json(scene_path)
    zones = list(session.exec(select(HazardZone).where(HazardZone.project_id == project_id)))
    return SceneResponse(
        project_id=project_id,
        scene_source=payload.get("scene_source", source),
        reconstruction_method=payload.get("reconstruction_method", "lidar_projection" if source == "lidar" else "colmap_sfm_mvs"),
        points=payload.get("points", []),
        full_points=payload.get("full_points", payload.get("points", [])),
        roof_removed_points=payload.get("roof_removed_points", payload.get("points", [])),
        floor_removed_points=payload.get("floor_removed_points", payload.get("points", [])),
        structure_points=payload.get("structure_points", payload.get("render_points", [])),
        render_points=payload.get("render_points", []),
        default_point_mode=payload.get("default_point_mode", "roof_removed" if payload.get("roof_removed_points") else "full"),
        trajectory=payload.get("trajectory", []),
        trajectory_timestamps=payload.get("trajectory_timestamps", []),
        trajectory_orientations=payload.get("trajectory_orientations", []),
        bounds=payload.get("bounds", {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}),
        full_bounds=payload.get("full_bounds", payload.get("bounds", {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]})),
        roof_removed_bounds=payload.get("roof_removed_bounds", payload.get("bounds", {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]})),
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



def _project_summary(project: Project) -> ProjectSummary:
    runtime_paths = project_runtime_paths(project)
    dataset_summary = read_json(runtime_paths["dataset_summary"]) if runtime_paths["dataset_summary"].exists() else {}
    analysis_summary = read_analysis_summary(project)
    scene_url = f"/api/projects/{project.id}/scene" if project.scene_path else None
    inspection_video_url = f"/artifacts/{project.id}/artifacts/inspection.mp4" if project.inspection_video_path else None
    analysis_mode = analysis_summary.get("analysis_mode")
    available_scene_sources = _available_scene_sources(project)
    return ProjectSummary(
        id=project.id or 0,
        name=project.name,
        status=project.status,
        bag_dir=project.bag_dir,
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
        available_scene_sources=available_scene_sources,
        default_scene_source="lidar",
        sfm_available="sfm" in available_scene_sources,
        provider_available=provider_available(),
        analysis_mode=analysis_mode if analysis_mode in {"demo", "provider"} else None,
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
    if not project.scene_path:
        return None
    return resolve_project_path(project.artifacts_dir, project.scene_path, "scenes/scene.json")


def _available_scene_sources(project: Project) -> list[Literal["lidar", "sfm"]]:
    sources: list[Literal["lidar", "sfm"]] = []
    if project.scene_path and resolve_project_path(project.artifacts_dir, project.scene_path, "scenes/scene.json").exists():
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
