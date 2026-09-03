from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from ..models import Finding, HazardRule, HazardZone, Project
from ..settings import YOLO_SAME_TIME_DEDUPE_WINDOW_MS
from .analysis_coordination import project_analysis_lock
from .analysis_summary import read_analysis_summary, write_analysis_summary
from .analysis_types import AnalysisRunResult, AnalysisVideoTarget, FindingSeed
from .evidence import cache_evidence_frames
from .provider import _dedupe_seeds, provider_available, provider_label, run_provider_analysis
from .provider_YOLO import provider_yolo_available, run_provider_yolo_analysis, run_provider_yolo_rtsp_live_analysis
from .rtsp_recorder import (
    ensure_rtsp_videos_for_analysis,
    is_rtsp_project,
    load_rtsp_project_settings,
    resolve_project_vehicle_id,
    should_use_rtsp_live_yolo_analysis,
)


def run_analysis(
    session: Session,
    project: Project,
    mode: str = "demo",
    model: str | None = None,
    *,
    record_fresh_rtsp: bool = False,
) -> list[Finding]:
    video_targets: list[AnalysisVideoTarget] | None = None
    rtsp_live_yolo = False
    if is_rtsp_project(project):
        settings = load_rtsp_project_settings(project)
        if mode == "provider_yolo" and should_use_rtsp_live_yolo_analysis(settings.rtsp_url):
            rtsp_live_yolo = True
        else:
            video_targets = ensure_rtsp_videos_for_analysis(session, project, record_fresh=record_fresh_rtsp)
            session.refresh(project)

    project_id = project.id or 0

    rules = list(
        session.exec(
            select(HazardRule)
            .where(HazardRule.project_id == (project.id or 0))
            .where(HazardRule.visual_detectable == True)
            .order_by(HazardRule.severity.desc(), HazardRule.rule_id)
        )
    )
    if not rules:
        result = AnalysisRunResult(
            status="indexed",
            findings=[],
            summary={
                "analysis_mode": mode,
                "analysis_provider": "demo" if mode == "demo" else ("yolo+" + provider_label() if mode == "provider_yolo" else provider_label()),
                "analysis_model": "demo" if mode == "demo" else model,
                "provider_available": provider_yolo_available(model) if mode == "provider_yolo" else provider_available(model),
                "status": "indexed",
                "notes": ["No visually detectable rules were available for this project."],
                "diagnostics": [],
            },
        )
        with project_analysis_lock(project_id):
            try:
                _delete_replaceable_findings(session, project_id)
                session.commit()
            except Exception:
                session.rollback()
                raise
            _finalize_project(session, project, result, persisted=[])
        return []

    from .maps import load_map_for_vehicle_id

    scene = load_map_for_vehicle_id(resolve_project_vehicle_id(project)) or {}
    trajectory = scene.get("trajectory", []) if isinstance(scene, dict) else []
    trajectory_timestamps = scene.get("trajectory_timestamps", []) if isinstance(scene, dict) else []
    start_ts = int(project.bag_start_ts or _now_ms())
    end_ts = int(project.bag_end_ts or (start_ts + 180_000))
    duration_ms = max(60_000, end_ts - start_ts)

    if mode in {"provider", "provider_yolo"}:
        project.status = "provider_analyzing"
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()

    try:
        if rtsp_live_yolo:
            result = run_provider_yolo_rtsp_live_analysis(project, rules, model_override=model)
        elif video_targets:
            partial_results = [
                _run_mode_analysis(
                    project,
                    rules,
                    mode=mode,
                    model=model,
                    video_target=target,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    duration_ms=duration_ms,
                )
                for target in video_targets
            ]
            result = _merge_analysis_results(mode=mode, model=model, partial_results=partial_results)
        else:
            result = _run_mode_analysis(
                project,
                rules,
                mode=mode,
                model=model,
                video_target=None,
                start_ts=start_ts,
                end_ts=end_ts,
                duration_ms=duration_ms,
            )
    except Exception as exc:
        session.rollback()
        failed = AnalysisRunResult(
            status="provider_failed" if mode != "demo" else "indexed",
            findings=[],
            summary={
                "analysis_mode": mode,
                "analysis_model": model,
                "status": "provider_failed" if mode != "demo" else "indexed",
                "notes": [],
                "diagnostics": [str(exc)],
            },
        )
        with project_analysis_lock(project_id):
            _finalize_project(session, project, failed, persisted=[])
        raise

    # A failed retry must not destroy the last successful batch result.
    if result.status == "provider_failed":
        with project_analysis_lock(project_id):
            _finalize_project(session, project, result, persisted=[])
        return []

    with project_analysis_lock(project_id):
        try:
            _delete_replaceable_findings(session, project_id)
            persisted: list[Finding] = []
            for index, seed in enumerate(result.findings):
                finding = Finding(
                    project_id=project_id,
                    finding_uid=f"{seed.rule_id}-{index:03d}",
                    rule_id=seed.rule_id,
                    title=seed.title,
                    time_start_ms=seed.time_start_ms,
                    time_end_ms=seed.time_end_ms,
                    evidence_frame_ts_json=json.dumps(sorted(set(seed.evidence_frame_ts))),
                    description=seed.description,
                    confidence=seed.confidence,
                    needs_review=True,
                    review_status="pending",
                    reviewer_notes="",
                    severity=seed.severity,
                    analysis_mode=mode,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(finding)
                session.flush()
                session.refresh(finding)
                _attach_zone(
                    session,
                    project_id,
                    finding,
                    trajectory,
                    trajectory_timestamps,
                    start_ts,
                    duration_ms,
                )
                persisted.append(finding)
            session.commit()
        except Exception:
            session.rollback()
            raise

        _finalize_project(session, project, result, persisted)
        return persisted


def _delete_replaceable_findings(session: Session, project_id: int) -> None:
    """Stage replacement of batch findings while preserving live-monitor rows."""
    monitor_finding_ids = [
        int(finding_id)
        for finding_id in session.exec(
            select(Finding.id).where(
                Finding.project_id == project_id,
                Finding.analysis_mode == "provider_yolo_monitor",
            )
        )
        if isinstance(finding_id, int)
    ]
    if monitor_finding_ids:
        session.exec(
            delete(HazardZone).where(
                HazardZone.project_id == project_id,
                HazardZone.finding_id.notin_(monitor_finding_ids),
            )
        )
    else:
        session.exec(delete(HazardZone).where(HazardZone.project_id == project_id))
    session.exec(
        delete(Finding).where(
            Finding.project_id == project_id,
            Finding.analysis_mode != "provider_yolo_monitor",
        )
    )


def _run_mode_analysis(
    project: Project,
    rules: list[HazardRule],
    *,
    mode: str,
    model: str | None,
    video_target: AnalysisVideoTarget | None,
    start_ts: int,
    end_ts: int,
    duration_ms: int,
) -> AnalysisRunResult:
    common_kwargs = {}
    if video_target is not None:
        common_kwargs = {
            "video_path": video_target.path,
            "video_start_ts": video_target.video_start_ts,
            "video_end_ts": video_target.video_end_ts,
            "video_label": video_target.label,
        }

    if mode == "provider_yolo":
        return run_provider_yolo_analysis(project, rules, model_override=model, **common_kwargs)
    if mode == "provider":
        return run_provider_analysis(project, rules, model_override=model, **common_kwargs)
    if video_target is not None:
        target_duration_ms = max(60_000, video_target.video_end_ts - video_target.video_start_ts)
        return _run_demo_analysis(
            rules,
            start_ts=video_target.video_start_ts,
            end_ts=video_target.video_end_ts,
            duration_ms=target_duration_ms,
        )
    return _run_demo_analysis(rules, start_ts=start_ts, end_ts=end_ts, duration_ms=duration_ms)


def _merge_analysis_results(*, mode: str, model: str | None, partial_results: list[AnalysisRunResult]) -> AnalysisRunResult:
    if not partial_results:
        return AnalysisRunResult(status="provider_failed" if mode != "demo" else "demo_analyzed", findings=[], summary={})

    merged_findings: list[FindingSeed] = []
    merged_notes: list[str] = []
    merged_diagnostics: list[str] = []
    successful_runs = 0
    clip_count = 0
    successful_clips = 0

    for result in partial_results:
        merged_findings.extend(result.findings)
        merged_notes.extend(str(note) for note in result.summary.get("notes", []) if note)
        merged_diagnostics.extend(str(item) for item in result.summary.get("diagnostics", []) if item)
        clip_count += int(result.summary.get("clip_count") or 0)
        successful_clips += int(result.summary.get("successful_clips") or 0)
        if result.status in {"provider_analyzed", "demo_analyzed"}:
            successful_runs += 1

    deduped = _dedupe_seeds(
        merged_findings,
        same_time_window_ms=YOLO_SAME_TIME_DEDUPE_WINDOW_MS if mode == "provider_yolo" else None,
    )
    if mode == "demo":
        status = "demo_analyzed" if successful_runs > 0 else "indexed"
    else:
        status = "provider_analyzed" if successful_clips > 0 else "provider_failed"

    base_summary = dict(partial_results[-1].summary)
    base_summary.update(
        {
            "status": status,
            "clip_count": clip_count,
            "successful_clips": successful_clips,
            "failed_clips": max(0, clip_count - successful_clips),
            "video_target_count": len(partial_results),
            "notes": list(dict.fromkeys(merged_notes)),
            "diagnostics": list(dict.fromkeys(merged_diagnostics)),
        }
    )
    if successful_runs > 0 and not deduped and mode != "demo":
        empty_note = (
            "YOLO + Provider completed successfully and returned no hazards."
            if mode == "provider_yolo"
            else "Provider completed successfully and returned no hazards."
        )
        base_summary["notes"] = list(base_summary.get("notes", [])) + [empty_note]

    return AnalysisRunResult(status=status, findings=deduped, summary=base_summary)


def _run_demo_analysis(
    rules: list[HazardRule],
    *,
    start_ts: int,
    end_ts: int,
    duration_ms: int,
) -> AnalysisRunResult:
    chosen_rules = rules[: min(6, len(rules))]
    seeds: list[FindingSeed] = []
    for index, rule in enumerate(chosen_rules):
        phase = 0.12 + index * 0.15
        window_start = start_ts + int(duration_ms * phase)
        window_end = min(end_ts, window_start + 10_000 + index * 2_000)
        evidence = [window_start, min(window_end, window_start + 2_500), min(window_end, window_start + 5_000)]
        seeds.append(
            FindingSeed(
                rule_id=rule.rule_id,
                title=_short_title(rule.hazard_desc),
                time_start_ms=window_start,
                time_end_ms=window_end,
                evidence_frame_ts=sorted(set(evidence)),
                description=(
                    "Demo analysis generated this finding from imported visual rules. "
                    "Replace the demo analyzer with a multimodal provider once clip-level inference is ready."
                ),
                confidence=round(0.66 + index * 0.05, 2),
                severity=rule.severity,
            )
        )
    return AnalysisRunResult(
        status="demo_analyzed",
        findings=seeds,
        summary={
            "analysis_mode": "demo",
            "analysis_provider": "demo",
            "analysis_model": "demo",
            "provider_available": provider_available(),
            "status": "demo_analyzed",
            "notes": [
                "Demo mode uses imported visual rules to publish placeholder findings.",
                "Switch to provider mode to call the DashScope-compatible multimodal model.",
            ],
            "diagnostics": [],
        },
    )


def _finalize_project(session: Session, project: Project, result: AnalysisRunResult, persisted: list[Finding]) -> None:
    project_id = project.id or 0
    total_findings = list(session.exec(select(Finding).where(Finding.project_id == project_id)))
    project.status = result.status
    # Include preserved monitor findings so the UI count stays accurate after batch analyze.
    project.findings_count = len(total_findings)
    project.updated_at = datetime.now(timezone.utc)
    session.add(project)
    session.commit()

    existing_summary = read_analysis_summary(project)
    merged_summary = dict(result.summary)
    monitor_segments = existing_summary.get("monitor_llm_segments")
    if isinstance(monitor_segments, list) and monitor_segments:
        merged_summary["monitor_llm_segments"] = monitor_segments
    write_analysis_summary(project, merged_summary)

    has_monitor_findings = any(
        getattr(finding, "analysis_mode", "") == "provider_yolo_monitor" for finding in total_findings
    )
    # Keep monitor evidence frames when batch findings are layered on top.
    if persisted:
        cache_evidence_frames(project, persisted, replace_all=not has_monitor_findings)


def _attach_zone(
    session: Session,
    project_id: int,
    finding: Finding,
    trajectory: list[list[float]],
    trajectory_timestamps: list[int],
    start_ts: int,
    duration_ms: int,
) -> HazardZone | None:
    if not trajectory:
        return None
    midpoint = int((finding.time_start_ms + finding.time_end_ms) / 2)
    point = _pick_trajectory_point(trajectory, trajectory_timestamps, midpoint, start_ts, duration_ms)
    zone = HazardZone(
        project_id=project_id,
        finding_id=finding.id or 0,
        center_x=float(point[0]),
        center_y=float(point[1]),
        center_z=float(point[2]),
        radius_m=2.8 if finding.severity == "critical" else 1.9,
        heading=0.0,
        related_pose_ts=midpoint,
    )
    session.add(zone)
    session.flush()
    return zone


def _pick_trajectory_point(
    trajectory: list[list[float]],
    trajectory_timestamps: list[int],
    target_ms: int,
    start_ts: int,
    duration_ms: int,
) -> list[float]:
    if not trajectory:
        return [0.0, 0.0, 0.0]
    if trajectory_timestamps and len(trajectory_timestamps) == len(trajectory):
        best_index = min(range(len(trajectory_timestamps)), key=lambda index: abs(trajectory_timestamps[index] - target_ms))
        return trajectory[best_index]
    relative = min(1.0, max(0.0, (target_ms - start_ts) / max(1, duration_ms)))
    return trajectory[min(len(trajectory) - 1, round(relative * (len(trajectory) - 1)))]


def _short_title(text: str) -> str:
    compact = text.replace("\u2605", "").replace("*", "").strip()
    return compact[:44] + ("..." if len(compact) > 44 else "")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
