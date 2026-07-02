from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ProjectImportRequest(BaseModel):
    name: str = Field(default="Inspection Workspace")
    bag_dir: str
    standards_dir: str


class AnalyzeRequest(BaseModel):
    mode: Literal["demo", "provider"] = "demo"
    model: Optional[str] = None


class FindingPatchRequest(BaseModel):
    review_status: Optional[str] = None
    reviewer_notes: Optional[str] = None
    needs_review: Optional[bool] = None


class BootstrapResponse(BaseModel):
    sample_bag_dir: Optional[str]
    sample_standards_dir: Optional[str]
    detected_bag_dirs: list[str]
    detected_standards_dirs: list[str]
    provider_available: bool = False
    default_analysis_model: Optional[str] = None
    supported_analysis_models: list[str] = Field(default_factory=list)


class RuntimeResetResponse(BaseModel):
    status: str
    removed_project_dirs: int
    removed_bytes: int


class SceneRebuildResponse(BaseModel):
    project_id: int
    status: str
    scene_url: Optional[str]
    colorized: bool
    color_source: str
    raw_point_count: int
    render_point_count: int
    updated_at: datetime
    notes: list[str] = Field(default_factory=list)


class ImageSceneRebuildResponse(BaseModel):
    project_id: int
    status: str
    scene_source: Literal["sfm"] = "sfm"
    selected_image_count: int
    registered_image_count: int
    dense_point_count: int
    aligned: bool
    alignment_rmse_m: Optional[float] = None
    notes: list[str] = Field(default_factory=list)


class ProjectSummary(BaseModel):
    id: int
    name: str
    status: str
    bag_dir: str
    standards_dir: str
    video_topic: Optional[str]
    point_topic: Optional[str]
    pose_topic: Optional[str]
    video_start_ts: Optional[int]
    video_end_ts: Optional[int]
    point_start_ts: Optional[int]
    point_end_ts: Optional[int]
    median_video_gap_ms: Optional[int]
    median_point_gap_ms: Optional[int]
    inferred_fps: Optional[float]
    bag_start_ts: Optional[int]
    bag_end_ts: Optional[int]
    bag_duration_ms: Optional[int]
    message_count: Optional[int]
    rules_count: int
    findings_count: int
    calibration_required: bool
    time_offset_ms: Optional[int]
    scene_url: Optional[str]
    inspection_video_url: Optional[str]
    available_scene_sources: list[Literal["lidar", "sfm"]] = Field(default_factory=lambda: ["lidar"])
    default_scene_source: Literal["lidar", "sfm"] = "lidar"
    sfm_available: bool = False
    provider_available: bool = False
    analysis_mode: Optional[Literal["demo", "provider"]] = None
    analysis_provider: Optional[str] = None
    analysis_model: Optional[str] = None
    analysis_notes: list[str] = Field(default_factory=list)
    analysis_diagnostics: list[str] = Field(default_factory=list)
    analysis_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RuleResponse(BaseModel):
    rule_id: str
    domain: str
    category: str
    object_name: str
    check_item: str
    checker_scope: str
    hazard_desc: str
    legal_basis: str
    evidence_objects: list[str]
    severity: str
    visual_detectable: bool
    source: str


class ZoneResponse(BaseModel):
    id: int
    finding_id: int
    center: list[float]
    radius_m: float
    heading: float
    related_pose_ts: int


class FindingResponse(BaseModel):
    id: int
    finding_uid: str
    rule_id: str
    title: str
    time_start_ms: int
    time_end_ms: int
    evidence_frame_ts: list[int]
    description: str
    confidence: float
    needs_review: bool
    review_status: str
    reviewer_notes: str
    severity: str
    analysis_mode: str
    legal_basis: str
    hazard_desc: str
    category: str
    checker_scope: str
    visual_detectable: bool
    zone: Optional[ZoneResponse]


class SceneResponse(BaseModel):
    project_id: int
    scene_source: Literal["lidar", "sfm"] = "lidar"
    reconstruction_method: str = "lidar_projection"
    points: list[list[float]]
    full_points: list[list[float]] = Field(default_factory=list)
    roof_removed_points: list[list[float]] = Field(default_factory=list)
    floor_removed_points: list[list[float]] = Field(default_factory=list)
    structure_points: list[list[float]] = Field(default_factory=list)
    render_points: list[list[float]] = Field(default_factory=list)
    default_point_mode: Literal["roof_removed", "full"] = "roof_removed"
    trajectory: list[list[float]]
    trajectory_timestamps: list[int] = Field(default_factory=list)
    trajectory_orientations: list[list[float]] = Field(default_factory=list)
    bounds: dict[str, list[float]]
    full_bounds: dict[str, list[float]] = Field(default_factory=lambda: {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]})
    roof_removed_bounds: dict[str, list[float]] = Field(default_factory=lambda: {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]})
    source_frame_count: int
    coordinate_frame: Literal["global", "sensor_local"]
    source_type: str
    raw_point_count: int = 0
    render_point_count: int = 0
    structure_point_count: int = 0
    colorized: bool = False
    color_source: str = "structure_enhanced"
    cut_height_default: float = 0.0
    floor_cut_default: float = 0.0
    scene_quality: dict[str, object] = Field(default_factory=dict)
    selected_image_count: int = 0
    registered_image_count: int = 0
    alignment_status: str = "not_applicable"
    alignment_rmse_m: Optional[float] = None
    notes: list[str]
    hazard_zones: list[ZoneResponse]
