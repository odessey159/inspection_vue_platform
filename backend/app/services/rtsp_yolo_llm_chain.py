"""LangChain workflow: RTSP YOLO monitor segment → rule context → vision LLM hazard review.

Triggered once per YOLO segment from ``rtsp_yolo_monitor``. Runs asynchronously so the
monitor can immediately start the next segment while the LLM review is in flight.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from ..db import engine
from ..models import Finding, HazardRule, Project
from ..settings import (
    RTSP_LLM_REVIEW_MAX_WORKERS,
    RULE_RAG_ENABLED,
    RTSP_RECORDINGS_DIR,
    RTSP_YOLO_MONITOR_LLM_ENABLED,
    RTSP_YOLO_MONITOR_LLM_ON_EMPTY,
    VISION_API_KEY,
    VISION_API_URL,
    VISION_ENABLE_THINKING,
    VISION_MAX_CLIP_BYTES,
    VISION_MAX_FINDINGS_PER_CLIP,
    VISION_MAX_RETRIES,
    VISION_REQUEST_TIMEOUT_SECONDS,
    VISION_TEMPERATURE,
    resolve_vision_model,
)
from .analysis_summary import read_analysis_summary, write_analysis_summary
from .analysis_types import FindingSeed
from .evidence import append_evidence_frames_from_clip
from .provider import (
    PreparedClip,
    ProviderResponsePayload,
    _build_prompt,
    _clip_findings_to_seeds,
    _compress_clip,
    _normalize_message_content,
    provider_available,
)
from .llm_output_log import write_llm_log
from .provider_YOLO import YoloClipResult, _build_yolo_prompt_section
from .rtsp_auto_analysis import list_rtsp_projects_for_storage_key
from .rule_retriever import hazard_rules_from_payloads, retrieve_rules_for_clip
from .rules import export_rules_payload
from .storage import read_json, remove_paths, write_json

logger = logging.getLogger(__name__)

_review_inflight: set[str] = set()
_review_lock = threading.Lock()
_review_executor = ThreadPoolExecutor(
    max_workers=max(1, RTSP_LLM_REVIEW_MAX_WORKERS),
    thread_name_prefix="rtsp-yolo-llm",
)


@dataclass
class SegmentReviewState:
    """Mutable state passed through the LangChain LCEL pipeline."""

    storage_key: str
    rtsp_url: str
    segment_index: int
    segment_start_sec: float
    segment_duration_sec: float
    yolo_result: YoloClipResult
    clip_path: Path | None = None
    video_start_ts: int = 0
    timeline_origin_ms: int = 0
    selected_model: str = ""
    projects: list[Project] = field(default_factory=list)
    rules: list[HazardRule] = field(default_factory=list)
    rules_by_id: dict[str, HazardRule] = field(default_factory=dict)
    retrieved_rules: list[HazardRule] = field(default_factory=list)
    retrieved_payloads: list[dict[str, Any]] = field(default_factory=list)
    prepared_clip: PreparedClip | None = None
    yolo_prompt_section: str = ""
    provider_payload: ProviderResponsePayload | None = None
    seeds_by_project: dict[int, list[FindingSeed]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    skipped: bool = False


def monitor_llm_enabled() -> bool:
    return RTSP_YOLO_MONITOR_LLM_ENABLED and provider_available()


def should_review_segment(yolo_result: YoloClipResult) -> bool:
    if not monitor_llm_enabled():
        return False
    if yolo_result.detections:
        return True
    return RTSP_YOLO_MONITOR_LLM_ON_EMPTY


def schedule_segment_llm_review(
    *,
    storage_key: str,
    rtsp_url: str,
    segment_index: int,
    segment_start_sec: float,
    segment_duration_sec: float,
    yolo_result: YoloClipResult,
    clip_path: Path | None = None,
    video_start_ts: int | None = None,
    timeline_origin_ms: int | None = None,
) -> None:
    """Fire-and-forget LangChain review for one YOLO monitor segment."""
    if not should_review_segment(yolo_result):
        return

    review_key = f"{storage_key}:{segment_index}"
    with _review_lock:
        if review_key in _review_inflight:
            return
        _review_inflight.add(review_key)

    resolved_start = video_start_ts or int(datetime.now(timezone.utc).timestamp() * 1000)
    _review_executor.submit(
        _run_segment_review_safe,
        storage_key=storage_key,
        rtsp_url=rtsp_url,
        segment_index=segment_index,
        segment_start_sec=segment_start_sec,
        segment_duration_sec=segment_duration_sec,
        yolo_result=yolo_result,
        clip_path=clip_path,
        video_start_ts=resolved_start,
        timeline_origin_ms=timeline_origin_ms or resolved_start,
        review_key=review_key,
    )


def run_segment_llm_review(
    *,
    storage_key: str,
    rtsp_url: str,
    segment_index: int,
    segment_start_sec: float,
    segment_duration_sec: float,
    yolo_result: YoloClipResult,
    clip_path: Path | None = None,
    video_start_ts: int | None = None,
    timeline_origin_ms: int | None = None,
) -> SegmentReviewState:
    """Synchronously run the LangChain YOLO→LLM segment review pipeline."""
    resolved_start = video_start_ts or int(datetime.now(timezone.utc).timestamp() * 1000)
    state = SegmentReviewState(
        storage_key=storage_key,
        rtsp_url=rtsp_url,
        segment_index=segment_index,
        segment_start_sec=segment_start_sec,
        segment_duration_sec=segment_duration_sec,
        yolo_result=yolo_result,
        clip_path=clip_path,
        video_start_ts=resolved_start,
        timeline_origin_ms=timeline_origin_ms or resolved_start,
        selected_model=resolve_vision_model(),
    )
    chain = build_segment_review_chain()
    return chain.invoke(state)


def build_segment_review_chain():
    """Build the LCEL chain: load context → retrieve rules → LLM → persist."""
    try:
        from langchain_core.runnables import RunnableLambda
    except ModuleNotFoundError as exc:  # pragma: no cover - optional until deps installed
        raise RuntimeError(
            "langchain-core is not installed. Install backend/requirements.txt to enable "
            "RTSP YOLO monitor LLM review."
        ) from exc

    return (
        RunnableLambda(_step_load_projects_and_rules)
        | RunnableLambda(_step_prepare_clip)
        | RunnableLambda(_step_retrieve_rules)
        | RunnableLambda(_step_invoke_llm)
        | RunnableLambda(_step_persist_findings)
    )


def _run_segment_review_safe(
    *,
    storage_key: str,
    rtsp_url: str,
    segment_index: int,
    segment_start_sec: float,
    segment_duration_sec: float,
    yolo_result: YoloClipResult,
    clip_path: Path | None,
    video_start_ts: int,
    timeline_origin_ms: int,
    review_key: str,
) -> None:
    try:
        state = run_segment_llm_review(
            storage_key=storage_key,
            rtsp_url=rtsp_url,
            segment_index=segment_index,
            segment_start_sec=segment_start_sec,
            segment_duration_sec=segment_duration_sec,
            yolo_result=yolo_result,
            clip_path=clip_path,
            video_start_ts=video_start_ts,
            timeline_origin_ms=timeline_origin_ms,
        )
        finding_count = sum(len(seeds) for seeds in state.seeds_by_project.values())
        logger.info(
            "RTSP YOLO LLM review finished for %s segment=%s findings=%s skipped=%s diagnostics=%s",
            storage_key,
            segment_index,
            finding_count,
            state.skipped,
            len(state.diagnostics),
        )
    except Exception:
        logger.exception(
            "RTSP YOLO LLM review failed for %s segment=%s",
            storage_key,
            segment_index,
        )
    finally:
        cleanup_segment_capture(clip_path)
        with _review_lock:
            _review_inflight.discard(review_key)


def _step_load_projects_and_rules(state: SegmentReviewState) -> SegmentReviewState:
    with Session(engine) as session:
        projects = list_rtsp_projects_for_storage_key(session, state.storage_key)
        if not projects:
            state.skipped = True
            state.diagnostics.append(
                f"No imported RTSP project matches storage_key={state.storage_key}"
            )
            return state

        # Detach project rows for use outside the session.
        state.projects = [
            Project.model_validate(project.model_dump()) for project in projects if project.id is not None
        ]
        primary = projects[0]
        rules = list(
            session.exec(
                select(HazardRule)
                .where(HazardRule.project_id == (primary.id or 0))
                .where(HazardRule.visual_detectable == True)  # noqa: E712
                .order_by(HazardRule.severity.desc(), HazardRule.rule_id)
            )
        )
        if not rules:
            state.skipped = True
            state.diagnostics.append("No visually detectable rules available for monitor LLM review")
            return state

        state.rules = [HazardRule.model_validate(rule.model_dump()) for rule in rules]
        state.rules_by_id = {rule.rule_id: rule for rule in state.rules}
        state.notes.append(
            f"Loaded {len(state.projects)} project(s) and {len(state.rules)} visual rule(s) "
            f"for segment {state.segment_index}."
        )
    return state


def _step_prepare_clip(state: SegmentReviewState) -> SegmentReviewState:
    if state.skipped:
        return state

    # Clip-relative offsets for the LLM; absolute stream time lives on start_ts_ms.
    if state.clip_path is None or not state.clip_path.exists():
        state.notes.append("No capture clip available; LLM will review YOLO detections as text-only hints.")
        state.prepared_clip = PreparedClip(
            index=state.segment_index,
            path=Path(""),
            start_offset_sec=0.0,
            duration_sec=state.segment_duration_sec,
            start_ts_ms=state.video_start_ts,
            byte_size=0,
            profile_name="text-only",
        )
        return state

    clip_dir = (
        RTSP_RECORDINGS_DIR
        / state.storage_key
        / "monitor_llm_clips"
        / f"segment_{state.segment_index:06d}"
    )
    clip_dir.mkdir(parents=True, exist_ok=True)
    prepared, error = _compress_clip(
        video_path=state.clip_path,
        clip_dir=clip_dir,
        index=state.segment_index,
        start_offset_sec=0.0,
        duration_sec=state.segment_duration_sec,
        start_ts_ms=state.video_start_ts,
    )
    if prepared is None:
        state.notes.append(f"Clip compression failed ({error}); falling back to text-only LLM review.")
        state.prepared_clip = PreparedClip(
            index=state.segment_index,
            path=Path(""),
            start_offset_sec=0.0,
            duration_sec=state.segment_duration_sec,
            start_ts_ms=state.video_start_ts,
            byte_size=0,
            profile_name="text-only",
        )
        return state

    state.prepared_clip = PreparedClip(
        index=prepared.index,
        path=prepared.path,
        start_offset_sec=0.0,
        duration_sec=prepared.duration_sec,
        start_ts_ms=state.video_start_ts,
        byte_size=prepared.byte_size,
        profile_name=prepared.profile_name,
    )
    return state


def _step_retrieve_rules(state: SegmentReviewState) -> SegmentReviewState:
    if state.skipped or state.prepared_clip is None:
        return state

    if not RULE_RAG_ENABLED:
        state.retrieved_rules = list(state.rules)
        state.retrieved_payloads = export_rules_payload(state.rules)
        state.yolo_prompt_section = _build_yolo_prompt_section(
            clip=state.prepared_clip,
            rules=state.retrieved_rules,
            yolo_result=state.yolo_result,
        )
        return state

    yolo_detections = [item.model_dump() for item in state.yolo_result.detections]
    scopes = [rule.checker_scope for rule in state.rules if rule.checker_scope and rule.checker_scope != "mixed"]
    checker_scope = None
    if scopes:
        counts: dict[str, int] = {}
        for scope in scopes:
            counts[scope] = counts.get(scope, 0) + 1
        checker_scope = max(counts, key=counts.get)

    retrieved_payloads = retrieve_rules_for_clip(
        yolo_detections=yolo_detections,
        checker_scope=checker_scope,
        domain="industrial-inspection",
    )
    retrieved_rules = hazard_rules_from_payloads(
        retrieved_payloads,
        project_id=state.projects[0].id or 0,
    )
    if not retrieved_rules:
        retrieved_rules = list(state.rules)
        retrieved_payloads = export_rules_payload(state.rules)
        state.notes.append("Rule RAG returned no matches; falling back to project visual rules.")

    state.retrieved_rules = retrieved_rules
    state.retrieved_payloads = retrieved_payloads
    state.rules_by_id.update({rule.rule_id: rule for rule in retrieved_rules})
    state.yolo_prompt_section = _build_yolo_prompt_section(
        clip=state.prepared_clip,
        rules=retrieved_rules,
        yolo_result=state.yolo_result,
        retrieved_payloads=retrieved_payloads,
    )
    return state


def _step_invoke_llm(state: SegmentReviewState) -> SegmentReviewState:
    if state.skipped or state.prepared_clip is None:
        return state

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover
        state.diagnostics.append(f"LangChain dependencies missing: {exc}")
        state.skipped = True
        return state

    prompt = _build_prompt(
        clip=state.prepared_clip,
        rules=state.retrieved_rules or state.rules,
        extra_context=state.yolo_prompt_section,
        retrieved_rules=RULE_RAG_ENABLED and bool(state.retrieved_payloads),
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if state.prepared_clip.path and state.prepared_clip.path.exists() and state.prepared_clip.byte_size > 0:
        video_bytes = state.prepared_clip.path.read_bytes()
        if len(video_bytes) > VISION_MAX_CLIP_BYTES:
            state.diagnostics.append(
                f"Prepared clip is {len(video_bytes) / (1024 * 1024):.2f} MB, above the configured limit"
            )
            state.skipped = True
            return state
        encoded = base64.b64encode(video_bytes).decode("ascii")
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": f"data:video/mp4;base64,{encoded}"},
            }
        )
    else:
        prompt = (
            f"{prompt}\n\n"
            "NOTE: No video clip is attached for this monitor segment. "
            "Treat YOLO detections as primary evidence. "
            "When detections clearly relate to listed rules "
            "(e.g. dust/dustcover→rule-104, sander/woodmachine/planer→rule-021, "
            "exit/passage→rule-056), report matching findings with clip-relative timestamps. "
            "Return empty findings only if detections are unrelated to any listed rule."
        )
        content = [{"type": "text", "text": prompt}]

    llm = ChatOpenAI(
        model=state.selected_model,
        api_key=VISION_API_KEY,
        base_url=VISION_API_URL.rstrip("/"),
        temperature=VISION_TEMPERATURE,
        max_tokens=1200,
        timeout=VISION_REQUEST_TIMEOUT_SECONDS,
        max_retries=max(0, VISION_MAX_RETRIES),
        model_kwargs={"response_format": {"type": "json_object"}},
        extra_body={"enable_thinking": VISION_ENABLE_THINKING},
    )

    messages = [
        SystemMessage(
            content=(
                "You are an industrial safety video reviewer. "
                "Report hazards supported by the video and/or YOLO detections. "
                "Only use the listed rule_id values. Return valid JSON only."
            )
        ),
        HumanMessage(content=content),
    ]

    try:
        response = llm.invoke(messages)
        raw_content = response.content
        json_payload = _normalize_message_content(raw_content)
        state.provider_payload = ProviderResponsePayload.model_validate(json_payload)
        seeds = _clip_findings_to_seeds(
            state.prepared_clip,
            state.provider_payload,
            state.rules_by_id,
        )
        for project in state.projects:
            if project.id is None:
                continue
            state.seeds_by_project[project.id] = list(seeds)
        state.notes.append(
            f"LangChain LLM ({state.selected_model}) returned {len(seeds)} finding seed(s) "
            f"for segment {state.segment_index} "
            f"(YOLO detections={len(state.yolo_result.detections)})."
        )
        if state.provider_payload.notes:
            state.notes.extend(f"llm: {note}" for note in state.provider_payload.notes)
        log_path = write_llm_log(
            source="rtsp_monitor",
            clip_index=f"{state.storage_key}_{state.segment_index:06d}",
            model=state.selected_model,
            raw_response=raw_content,
            parsed_payload=state.provider_payload,
            notes=list(state.notes),
            diagnostics=list(state.diagnostics),
            prompt_section=state.yolo_prompt_section,
            extra={
                "storage_key": state.storage_key,
                "rtsp_url": state.rtsp_url,
                "segment_index": state.segment_index,
                "segment_start_sec": state.segment_start_sec,
                "segment_duration_sec": state.segment_duration_sec,
                "yolo_detections": len(state.yolo_result.detections),
                "findings": len(seeds),
                "has_video": bool(
                    state.prepared_clip.path
                    and state.prepared_clip.path.exists()
                    and state.prepared_clip.byte_size > 0
                ),
            },
        )
        state.notes.append(f"LLM response logged to {log_path}")
    except Exception as exc:
        state.diagnostics.append(f"LangChain LLM invoke failed: {exc}")
        try:
            write_llm_log(
                source="rtsp_monitor",
                clip_index=f"{state.storage_key}_{state.segment_index:06d}",
                model=state.selected_model,
                raw_response="",
                parsed_payload=None,
                notes=list(state.notes),
                diagnostics=list(state.diagnostics),
                prompt_section=state.yolo_prompt_section,
                extra={
                    "storage_key": state.storage_key,
                    "rtsp_url": state.rtsp_url,
                    "segment_index": state.segment_index,
                    "segment_start_sec": state.segment_start_sec,
                    "segment_duration_sec": state.segment_duration_sec,
                    "yolo_detections": len(state.yolo_result.detections),
                    "error": str(exc),
                },
            )
        except Exception:
            logger.exception("Failed to write LLM error log for segment %s", state.segment_index)
        state.skipped = True
        return state

    return state


def _step_persist_findings(state: SegmentReviewState) -> SegmentReviewState:
    if state.skipped:
        return state

    for project in state.projects:
        if project.id is None:
            continue
        seeds = state.seeds_by_project.get(project.id) or []
        evidence_clip: Path | None = None
        if (
            state.prepared_clip is not None
            and state.prepared_clip.path
            and state.prepared_clip.byte_size > 0
            and state.prepared_clip.path.exists()
        ):
            evidence_clip = state.prepared_clip.path
        elif state.clip_path is not None and state.clip_path.exists():
            evidence_clip = state.clip_path
        persisted = _persist_seeds_for_project(
            project_id=project.id,
            segment_index=state.segment_index,
            seeds=seeds,
            notes=state.notes,
            diagnostics=state.diagnostics,
            selected_model=state.selected_model,
            yolo_detection_count=len(state.yolo_result.detections),
            timeline_origin_ms=state.timeline_origin_ms,
            segment_end_ts_ms=state.video_start_ts + int(round(state.segment_duration_sec * 1000)),
            clip_path=evidence_clip,
            clip_start_ts_ms=state.video_start_ts,
        )
        state.notes.append(
            f"project-{project.id}: persisted {len(persisted)} monitor finding(s) "
            f"from segment {state.segment_index}."
        )
    return state


def _sync_project_video_timeline(
    project: Project,
    *,
    timeline_origin_ms: int,
    segment_end_ts_ms: int,
) -> None:
    """Extend project end timestamps for live monitor without clobbering import clocks."""
    if timeline_origin_ms <= 0:
        return

    summary_path = Path(project.artifacts_dir) / "summaries" / "dataset_summary.json"
    summary: dict[str, object] = {}
    if summary_path.exists():
        try:
            loaded = read_json(summary_path)
            if isinstance(loaded, dict):
                summary = loaded
        except Exception:
            summary = {}

    previous_start = int(summary.get("video_start_ts") or project.bag_start_ts or 0)
    previous_end = int(summary.get("video_end_ts") or project.bag_end_ts or 0)
    # Keep the original import/recording start; only extend the end as monitoring continues.
    if previous_start > 0:
        summary["video_start_ts"] = previous_start
    else:
        summary["video_start_ts"] = timeline_origin_ms
        project.bag_start_ts = timeline_origin_ms
    summary["video_end_ts"] = max(previous_end, segment_end_ts_ms, int(summary["video_start_ts"]))
    if "point_start_ts" not in summary:
        summary["point_start_ts"] = summary["video_start_ts"]
    summary["point_end_ts"] = summary["video_end_ts"]
    summary["source_type"] = summary.get("source_type") or "rtsp_monitor"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(summary_path, summary)

    if project.bag_start_ts is None or project.bag_start_ts <= 0:
        project.bag_start_ts = int(summary["video_start_ts"])
    project.bag_end_ts = int(summary["video_end_ts"])


def _monitor_seed_is_duplicate(
    existing: list[Finding],
    seed: FindingSeed,
    *,
    window_ms: int = 15_000,
) -> bool:
    for finding in existing:
        if finding.rule_id != seed.rule_id:
            continue
        if abs(int(finding.time_start_ms) - int(seed.time_start_ms)) <= window_ms:
            return True
    return False


def _persist_seeds_for_project(
    *,
    project_id: int,
    segment_index: int,
    seeds: list[FindingSeed],
    notes: list[str],
    diagnostics: list[str],
    selected_model: str,
    yolo_detection_count: int,
    timeline_origin_ms: int,
    segment_end_ts_ms: int,
    clip_path: Path | None,
    clip_start_ts_ms: int,
) -> list[Finding]:
    """Append segment findings without wiping prior analysis results."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if project is None:
            return []

        _sync_project_video_timeline(
            project,
            timeline_origin_ms=timeline_origin_ms,
            segment_end_ts_ms=segment_end_ts_ms,
        )

        existing_findings = list(
            session.exec(select(Finding).where(Finding.project_id == project_id)).all()
        )
        trajectory: list[list[float]] = []
        trajectory_timestamps: list[int] = []
        scene_start_ts = int(project.bag_start_ts or timeline_origin_ms or 0)
        scene_duration_ms = max(60_000, int((project.bag_end_ts or segment_end_ts_ms) - scene_start_ts))
        if project.scene_path:
            try:
                from .storage import resolve_project_path

                scene = read_json(
                    resolve_project_path(project.artifacts_dir, project.scene_path, "scenes/scene.json")
                )
                trajectory = list(scene.get("trajectory") or [])
                trajectory_timestamps = [int(value) for value in (scene.get("trajectory_timestamps") or [])]
                if trajectory_timestamps:
                    scene_start_ts = int(trajectory_timestamps[0])
                    scene_duration_ms = max(60_000, int(trajectory_timestamps[-1]) - scene_start_ts)
            except Exception:
                logger.exception("Failed to load scene for monitor zone attach on project %s", project_id)

        persisted: list[Finding] = []
        skipped_duplicates = 0
        for index, seed in enumerate(seeds[:VISION_MAX_FINDINGS_PER_CLIP]):
            if _monitor_seed_is_duplicate(existing_findings + persisted, seed):
                skipped_duplicates += 1
                continue
            finding = Finding(
                project_id=project_id,
                finding_uid=f"rtsp-mon-{segment_index:06d}-{seed.rule_id}-{index:03d}-{int(seed.time_start_ms)}",
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
                analysis_mode="provider_yolo_monitor",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(finding)
            session.flush()
            session.refresh(finding)
            try:
                from .analysis import _attach_zone

                _attach_zone(
                    session,
                    project_id,
                    finding,
                    trajectory,
                    trajectory_timestamps,
                    scene_start_ts,
                    scene_duration_ms,
                )
            except Exception:
                logger.exception("Failed to attach hazard zone for monitor finding %s", finding.id)
            persisted.append(finding)

        if skipped_duplicates:
            notes.append(
                f"Skipped {skipped_duplicates} duplicate monitor finding(s) in segment {segment_index}."
            )

        total_findings_count = session.exec(
            select(func.count()).select_from(Finding).where(Finding.project_id == project_id)
        ).one()
        project.findings_count = total_findings_count
        if project.status not in {"indexing"}:
            project.status = "provider_analyzed" if project.findings_count > 0 else "indexed"
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()
        session.refresh(project)

        summary = read_analysis_summary(project)
        monitor_entries = list(summary.get("monitor_llm_segments") or [])
        if not isinstance(monitor_entries, list):
            monitor_entries = []
        monitor_entries.append(
            {
                "segment_index": segment_index,
                "yolo_detections": yolo_detection_count,
                "findings": len(persisted),
                "skipped_duplicates": skipped_duplicates,
                "model": selected_model,
                "timeline_origin_ms": timeline_origin_ms,
                "notes": notes[-8:],
                "diagnostics": diagnostics[-8:],
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        summary["monitor_llm_segments"] = monitor_entries[-40:]
        summary["analysis_mode"] = "provider_yolo_monitor"
        summary["analysis_provider"] = "yolo+langchain+dashscope"
        summary["analysis_model"] = selected_model
        summary["status"] = project.status
        write_analysis_summary(project, summary)

        if persisted:
            try:
                if clip_path is not None and clip_path.exists():
                    append_evidence_frames_from_clip(
                        project,
                        persisted,
                        clip_path=clip_path,
                        clip_start_ts_ms=clip_start_ts_ms,
                    )
                else:
                    # Text-only monitor findings must not seek the old imported inspection.mp4
                    # with live-stream timestamps.
                    logger.info(
                        "Skipping evidence-frame cache for project %s segment %s (no monitor clip)",
                        project_id,
                        segment_index,
                    )
            except Exception:
                logger.exception("Failed to cache evidence frames for project %s monitor findings", project_id)

        return persisted


def monitor_clip_capture_dir(storage_key: str) -> Path:
    path = RTSP_RECORDINGS_DIR / storage_key / "monitor_captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_segment_capture(path: Path | None) -> None:
    if path is None:
        return
    remove_paths([path])
