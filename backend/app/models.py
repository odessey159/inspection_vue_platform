from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    status: str = Field(default="indexed", index=True)
    vehicle_id: Optional[str] = Field(default=None, index=True)
    bag_dir: str
    standards_dir: str
    artifacts_dir: str
    video_topic: Optional[str] = Field(default=None, index=True)
    point_topic: Optional[str] = Field(default=None, index=True)
    pose_topic: Optional[str] = Field(default=None, index=True)
    bag_start_ts: Optional[int] = Field(default=None, index=True)
    bag_end_ts: Optional[int] = Field(default=None, index=True)
    bag_duration_ms: Optional[int] = None
    message_count: Optional[int] = None
    rules_count: int = Field(default=0)
    findings_count: int = Field(default=0)
    calibration_required: bool = Field(default=True)
    time_offset_ms: Optional[int] = None
    rosbag_summary_path: Optional[str] = None
    rules_path: Optional[str] = None
    scene_path: Optional[str] = None
    inspection_video_path: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)


class HazardRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    rule_id: str = Field(index=True)
    domain: str
    category: str
    object_name: str
    check_item: str
    checker_scope: str = Field(default="")
    hazard_desc: str
    legal_basis: str
    evidence_objects_json: str = Field(default="[]")
    severity: str = Field(default="medium", index=True)
    visual_detectable: bool = Field(default=False, index=True)
    source: str = Field(default="xlsx")


class Finding(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    finding_uid: str = Field(index=True)
    rule_id: str = Field(index=True)
    title: str
    time_start_ms: int = Field(index=True)
    time_end_ms: int = Field(index=True)
    evidence_frame_ts_json: str = Field(default="[]")
    description: str
    confidence: float
    needs_review: bool = Field(default=True, index=True)
    review_status: str = Field(default="pending", index=True)
    reviewer_notes: str = Field(default="")
    severity: str = Field(default="medium", index=True)
    analysis_mode: str = Field(default="demo")
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)


class HazardZone(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    finding_id: int = Field(index=True)
    center_x: float
    center_y: float
    center_z: float
    radius_m: float
    heading: float
    related_pose_ts: int = Field(index=True)
