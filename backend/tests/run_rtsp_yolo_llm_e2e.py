from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.db import create_db_and_tables, engine
from app.models import Finding, HazardRule, Project
from app.services.provider import provider_available
from app.services.provider_YOLO import invoke_yolo_rtsp_segment, yolo_available
from app.services.rtsp_recorder import (
    capture_monitor_segment_clip,
    resolve_storage_key_for_rtsp_url,
)
from app.services.rtsp_yolo_llm_chain import (
    cleanup_segment_capture,
    monitor_clip_capture_dir,
    run_segment_llm_review,
)
from app.settings import DATABASE_PATH, LLM_LOG_DIR, PROJECTS_DIR, RTSP_RECORDINGS_DIR


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one real RTSP -> remote YOLO -> local vision-LLM monitor segment."
    )
    parser.add_argument("--rtsp-url", default="rtsp://127.0.0.1:18554/live")
    parser.add_argument("--duration", type=float, default=5.0)
    return parser.parse_args()


def _seed_project(rtsp_url: str, storage_key: str) -> Project:
    artifacts_dir = PROJECTS_DIR / "e2e-rtsp-yolo-llm"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with Session(engine) as session:
        project = Project(
            name="RTSP YOLO LLM isolated E2E",
            status="indexed",
            vehicle_id=storage_key,
            bag_dir=rtsp_url,
            standards_dir=str(artifacts_dir / "standards"),
            artifacts_dir=str(artifacts_dir),
            calibration_required=False,
            bag_start_ts=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        session.add(project)
        session.commit()
        session.refresh(project)

        rules = [
            HazardRule(
                project_id=project.id or 0,
                rule_id="e2e-human-restricted-area",
                domain="industrial-inspection",
                category="restricted-area",
                object_name="human",
                check_item="restricted area occupancy",
                checker_scope="mixed",
                hazard_desc=(
                    "For this isolated integration test, any detected human means that "
                    "unauthorized personnel entered the restricted inspection area."
                ),
                legal_basis="Synthetic E2E validation rule; not a production regulation.",
                evidence_objects_json='["human"]',
                severity="high",
                visual_detectable=True,
                source="e2e-test",
            ),
            HazardRule(
                project_id=project.id or 0,
                rule_id="e2e-dust-control",
                domain="industrial-inspection",
                category="dust-control",
                object_name="dustcover",
                check_item="dust control enclosure",
                checker_scope="mixed",
                hazard_desc="Report only visible dust leakage or an obviously open dust cover.",
                legal_basis="Synthetic E2E validation rule; not a production regulation.",
                evidence_objects_json='["dust", "dustcover", "dustduct", "dustremover"]',
                severity="medium",
                visual_detectable=True,
                source="e2e-test",
            ),
        ]
        session.add_all(rules)
        project.rules_count = len(rules)
        session.add(project)
        session.commit()
        session.refresh(project)
        return Project.model_validate(project.model_dump())


def main() -> int:
    args = _parse_args()
    if args.duration < 1.0:
        raise ValueError("--duration must be at least 1 second")
    if not yolo_available():
        raise RuntimeError("YOLO_API_URL is not configured")
    if not provider_available():
        raise RuntimeError("The local vision provider is not configured")

    create_db_and_tables()
    storage_key = resolve_storage_key_for_rtsp_url(args.rtsp_url)
    project = _seed_project(args.rtsp_url, storage_key)
    capture_path = monitor_clip_capture_dir(storage_key) / "e2e-segment.mp4"
    cleanup_segment_capture(capture_path)

    started_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    yolo_result = invoke_yolo_rtsp_segment(
        rtsp_url=args.rtsp_url,
        duration_sec=args.duration,
        clip_index="rtsp_e2e_000000",
        rtsp_transport="tcp",
        segment_index=0,
        segment_start_sec=0.0,
    )
    captured_path = capture_monitor_segment_clip(
        storage_key=storage_key,
        output_path=capture_path,
        segment_start_sec=0.0,
        duration_sec=args.duration,
        rtsp_url=args.rtsp_url,
        rtsp_transport="tcp",
    )

    try:
        state = run_segment_llm_review(
            storage_key=storage_key,
            rtsp_url=args.rtsp_url,
            segment_index=0,
            segment_start_sec=0.0,
            segment_duration_sec=args.duration,
            yolo_result=yolo_result,
            clip_path=captured_path,
            video_start_ts=started_ms,
            timeline_origin_ms=started_ms,
        )

        with Session(engine) as session:
            findings = list(
                session.exec(
                    select(Finding)
                    .where(Finding.project_id == (project.id or 0))
                    .order_by(Finding.id)
                ).all()
            )

        class_counts: dict[str, int] = {}
        view_counts: dict[str, int] = {}
        for detection in yolo_result.detections:
            class_counts[detection.class_name] = class_counts.get(detection.class_name, 0) + 1
            view = detection.camera_view or "unassigned"
            view_counts[view] = view_counts.get(view, 0) + 1

        payload = {
            "database_path": str(DATABASE_PATH),
            "projects_dir": str(PROJECTS_DIR),
            "recordings_dir": str(RTSP_RECORDINGS_DIR),
            "llm_log_dir": str(LLM_LOG_DIR),
            "storage_key": storage_key,
            "project_id": project.id,
            "yolo": {
                "detections": len(yolo_result.detections),
                "classes": class_counts,
                "camera_views": view_counts,
                "notes": yolo_result.notes,
            },
            "capture": {
                "exists": captured_path.is_file(),
                "bytes": captured_path.stat().st_size if captured_path.is_file() else 0,
            },
            "llm": {
                "model": state.selected_model,
                "skipped": state.skipped,
                "prepared_clip_bytes": (
                    state.prepared_clip.byte_size if state.prepared_clip is not None else 0
                ),
                "returned_finding_seeds": sum(
                    len(items) for items in state.seeds_by_project.values()
                ),
                "diagnostics": state.diagnostics,
                "notes": state.notes,
            },
            "persisted_findings": [
                {
                    "rule_id": finding.rule_id,
                    "title": finding.title,
                    "confidence": finding.confidence,
                    "analysis_mode": finding.analysis_mode,
                }
                for finding in findings
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not state.skipped else 2
    finally:
        cleanup_segment_capture(capture_path)


if __name__ == "__main__":
    raise SystemExit(main())
