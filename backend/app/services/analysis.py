from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from ..models import Finding, HazardRule, HazardZone, Project
from .analysis_summary import write_analysis_summary
from .analysis_types import AnalysisRunResult, FindingSeed
from .evidence import cache_evidence_frames
from .provider import provider_available, provider_label, run_provider_analysis
from .storage import read_json, resolve_project_path



def run_analysis(session: Session, project: Project, mode: str = "demo", model: str | None = None) -> list[Finding]:
    session.exec(delete(HazardZone).where(HazardZone.project_id == (project.id or 0)))
    session.exec(delete(Finding).where(Finding.project_id == (project.id or 0)))
    session.commit()

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
                "analysis_provider": "demo" if mode == "demo" else provider_label(),
                "analysis_model": "demo" if mode == "demo" else model,
                "provider_available": provider_available(model),
                "status": "indexed",
                "notes": ["No visually detectable rules were available for this project."],
                "diagnostics": [],
            },
        )
        _finalize_project(session, project, result, persisted=[])
        return []

    if not project.scene_path:
        raise FileNotFoundError("Scene payload is missing")

    scene = read_json(resolve_project_path(project.artifacts_dir, project.scene_path, "scenes/scene.json"))
    trajectory = scene.get("trajectory", [])
    trajectory_timestamps = scene.get("trajectory_timestamps", [])
    start_ts = int(scene.get("trajectory_timestamps", [project.bag_start_ts or _now_ms()])[0] or project.bag_start_ts or _now_ms())
    end_ts = int(scene.get("trajectory_timestamps", [project.bag_end_ts or (start_ts + 180_000)])[-1] or project.bag_end_ts or (start_ts + 180_000))
    duration_ms = max(60_000, end_ts - start_ts)

    if mode == "provider":
        project.status = "provider_analyzing"
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()
        result = run_provider_analysis(project, rules, model_override=model)
    else:
        result = _run_demo_analysis(rules, start_ts=start_ts, end_ts=end_ts, duration_ms=duration_ms)

    persisted: list[Finding] = []
    for index, seed in enumerate(result.findings):
        finding = Finding(
            project_id=project.id or 0,
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
        session.commit()
        session.refresh(finding)
        _attach_zone(session, project.id or 0, finding, trajectory, trajectory_timestamps, start_ts, duration_ms)
        persisted.append(finding)

    _finalize_project(session, project, result, persisted)
    return persisted



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
    project.status = result.status
    project.findings_count = len(persisted)
    project.updated_at = datetime.now(timezone.utc)
    session.add(project)
    session.commit()
    write_analysis_summary(project, result.summary)
    cache_evidence_frames(project, persisted)



def _attach_zone(
    session: Session,
    project_id: int,
    finding: Finding,
    trajectory: list[list[float]],
    trajectory_timestamps: list[int],
    start_ts: int,
    duration_ms: int,
) -> None:
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
    session.commit()



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
