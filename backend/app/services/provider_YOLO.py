"""YOLO + vision-provider analysis: clip upload, segmented RTSP live, and optional Rule RAG."""

from __future__ import annotations

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

from pydantic import BaseModel, Field

from ..models import HazardRule, Project
from ..settings import (
    RULE_RAG_ENABLED,
    YOLO_API_KEY,
    YOLO_API_URL,
    YOLO_CONFIDENCE_THRESHOLD,
    YOLO_DETECT_PATH,
    YOLO_FAIL_OPEN,
    YOLO_MAX_RETRIES,
    YOLO_REQUEST_TIMEOUT_SECONDS,
    YOLO_RTSP_DEFAULT_DURATION_SEC,
    YOLO_RTSP_DETECT_PATH,
    YOLO_RTSP_MAX_DURATION_SEC,
    YOLO_RTSP_SEGMENT_SECONDS,
    YOLO_RTSP_TRANSPORT,
    resolve_vision_model,
)
from .rule_retriever import (
    hazard_rules_from_payloads,
    retrieve_rules_for_clip,
    save_retrieval_artifact,
)
from .analysis_types import AnalysisRunResult, FindingSeed
from .provider import (
    PreparedClip,
    ProviderResponsePayload,
    _clip_findings_to_seeds,
    _dedupe_seeds,
    _invoke_dashscope,
    _prepare_clips,
    provider_available,
    provider_label,
)
from .rtsp_recorder import (
    DEFAULT_RTSP_TRANSPORT,
    extract_video_clip,
    load_rtsp_project_settings,
    probe_recorded_video,
    record_rtsp_stream,
    recording_result_from_path,
    resolve_recording_rtsp_url,
    resolve_storage_key_for_rtsp_url,
    resolve_yolo_client_rtsp_url,
    wait_for_completed_recording,
)
from .rules import export_rules_payload
from .storage import read_json, remove_paths, resolve_project_path


class YoloDetectionPayload(BaseModel):
    class_name: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    time_sec: float | None = None
    bbox: list[float] = Field(default_factory=list)


class YoloClipResponsePayload(BaseModel):
    detections: list[YoloDetectionPayload] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@dataclass
class YoloClipResult:
    detections: list[YoloDetectionPayload] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_payload: dict[str, object] = field(default_factory=dict)


def yolo_available() -> bool:
    return bool(YOLO_API_URL)


def provider_yolo_available(model_override: str | None = None) -> bool:
    return yolo_available()


def provider_yolo_label() -> str:
    return f"yolo+{provider_label()}"


def run_provider_yolo_rtsp_live_analysis(
    project: Project,
    rules: list[HazardRule],
    model_override: str | None = None,
    *,
    rtsp_url: str | None = None,
    rtsp_transport: str | None = None,
) -> AnalysisRunResult:
    """Analyze a publishing RTSP stream via segmented YOLO calls, then LLM per virtual clip."""
    diagnostics: list[str] = []
    notes: list[str] = []
    selected_model = resolve_vision_model(model_override)
    validation_error = _validate_provider_yolo_prerequisites(selected_model)
    if validation_error is not None:
        status, diagnostics = validation_error
        return _result(status, [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)
    _append_vision_provider_warning(notes, selected_model)

    settings = load_rtsp_project_settings(project)
    source_url = (rtsp_url or settings.rtsp_url).strip()
    transport = (rtsp_transport or settings.rtsp_transport or YOLO_RTSP_TRANSPORT or DEFAULT_RTSP_TRANSPORT).strip().lower()

    try:
        resolved_url = resolve_recording_rtsp_url(source_url)
    except (RuntimeError, ValueError) as exc:
        diagnostics.append(f"RTSP stream is not reachable: {exc}")
        return _result(
            "provider_failed",
            [],
            diagnostics,
            notes,
            clip_count=0,
            successful_clips=0,
            selected_model=selected_model,
            rtsp_analysis_mode="live",
        )

    duration_sec = _resolve_rtsp_live_duration_sec(project)
    video_start_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    video_end_ts = video_start_ts + int(round(duration_sec * 1000))

    clip_dir = Path(project.artifacts_dir) / "artifacts" / "analysis_clips" / "rtsp_live"
    yolo_dir = Path(project.artifacts_dir) / "artifacts" / "yolo_results" / "rtsp_live"
    capture_dir = clip_dir / "capture"
    clip_dir.mkdir(parents=True, exist_ok=True)
    yolo_dir.mkdir(parents=True, exist_ok=True)
    capture_dir.mkdir(parents=True, exist_ok=True)
    remove_paths(list(clip_dir.glob("clip_*.mp4")))
    remove_paths(list(yolo_dir.glob("clip_*.json")))
    remove_paths(list(yolo_dir.glob("segment_*.json")))

    capture_path = capture_dir / "live_capture.mp4"
    remove_paths([capture_path])

    notes.append(
        f"RTSP live analysis started for `{resolved_url}` "
        f"(duration={duration_sec:.1f}s, segment={YOLO_RTSP_SEGMENT_SECONDS:.0f}s, transport={transport})."
    )

    from .rtsp_watchdog import get_active_recording, is_recording_active_for_rtsp_url

    watchdog_recording = is_recording_active_for_rtsp_url(resolved_url)
    analysis_started_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    storage_key = resolve_storage_key_for_rtsp_url(resolved_url)
    active_recording = get_active_recording(storage_key) if watchdog_recording else None
    analysis_start_offset_sec = 0.0
    if active_recording is not None and active_recording.started_at_ms > 0:
        analysis_start_offset_sec = max(
            0.0,
            (analysis_started_ms - active_recording.started_at_ms) / 1000.0,
        )

    try:
        if watchdog_recording:
            notes.append(
                "Watchdog is already recording this RTSP stream; YOLO will read the live stream "
                "and provider clips will reuse the shared watchdog capture."
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                yolo_future = executor.submit(
                    _invoke_yolo_rtsp_segments,
                    rtsp_url=resolved_url,
                    total_duration_sec=duration_sec,
                    rtsp_transport=transport,
                    output_dir=yolo_dir,
                )
                live_yolo_result = yolo_future.result()
            wall_elapsed_sec = max(
                duration_sec,
                (int(datetime.now(timezone.utc).timestamp() * 1000) - analysis_started_ms) / 1000.0,
            )
            recording = _recording_from_active_watchdog(
                rtsp_url=resolved_url,
                capture_path=capture_path,
                active=active_recording,
                start_offset_sec=analysis_start_offset_sec,
                duration_sec=wall_elapsed_sec,
                analysis_started_ms=analysis_started_ms,
            )
        else:
            with ThreadPoolExecutor(max_workers=2) as executor:
                record_future = executor.submit(
                    record_rtsp_stream,
                    rtsp_url=resolved_url,
                    output_path=capture_path,
                    duration_sec=duration_sec,
                    rtsp_transport=transport,
                    skip_server_check=True,
                )
                yolo_future = executor.submit(
                    _invoke_yolo_rtsp_segments,
                    rtsp_url=resolved_url,
                    total_duration_sec=duration_sec,
                    rtsp_transport=transport,
                    output_dir=yolo_dir,
                )
                live_yolo_result = yolo_future.result()
                recording = record_future.result()
    except Exception as exc:
        diagnostics.append(f"RTSP live capture failed: {exc}")
        remove_paths([capture_path])
        return _result(
            "provider_failed",
            [],
            diagnostics,
            notes,
            clip_count=0,
            successful_clips=0,
            selected_model=selected_model,
            rtsp_analysis_mode="live",
        )

    video_start_ts = recording.video_start_ts
    video_end_ts = recording.video_end_ts

    prepared_clips, clip_diagnostics = _prepare_clips(
        video_path=recording.playback_path,
        clip_dir=clip_dir,
        video_start_ts=video_start_ts,
        video_end_ts=video_end_ts,
    )
    diagnostics.extend(clip_diagnostics)
    if not prepared_clips:
        diagnostics.append("No provider clips were prepared from the RTSP live capture")
        return _result(
            "provider_failed",
            [],
            diagnostics,
            notes,
            clip_count=0,
            successful_clips=0,
            selected_model=selected_model,
            rtsp_analysis_mode="live",
        )

    if live_yolo_result.notes:
        notes.extend(f"rtsp-live/yolo: {note}" for note in live_yolo_result.notes)
    notes.append(
        f"RTSP live YOLO returned {len(live_yolo_result.detections)} detections across "
        f"{duration_sec:.1f}s in {live_yolo_result.raw_payload.get('segment_count', 1)} segment response(s)."
    )

    rules_by_id = {rule.rule_id: rule for rule in rules}
    seeds: list[FindingSeed] = []
    successful_clips = 0

    for clip in prepared_clips:
        clip_yolo_result = _split_yolo_result_for_clip(live_yolo_result, clip)
        try:
            if RULE_RAG_ENABLED:
                clip_seeds, yolo_result, payload, yolo_prompt_section, retrieved_payloads = (
                    _analyze_clip_with_yolo_rag_then_provider(
                        clip=clip,
                        rules=rules,
                        rules_by_id=rules_by_id,
                        selected_model=selected_model,
                        yolo_result=clip_yolo_result,
                    )
                )
                _persist_yolo_result(
                    yolo_dir,
                    clip,
                    yolo_result,
                    yolo_prompt_section,
                    retrieved_payloads=retrieved_payloads,
                )
            else:
                clip_seeds, yolo_result, payload, yolo_prompt_section = _analyze_clip_with_yolo_then_provider(
                    clip=clip,
                    rules=rules,
                    rules_by_id=rules_by_id,
                    selected_model=selected_model,
                    yolo_result=clip_yolo_result,
                )
                _persist_yolo_result(yolo_dir, clip, yolo_result, yolo_prompt_section)
            seeds.extend(clip_seeds)
            successful_clips += 1
            notes.append(
                f"clip-{clip.index:02d}: RTSP-live YOLO found {len(yolo_result.detections)} detections; "
                f"{selected_model} returned {len(clip_seeds)} findings."
            )
            if payload.notes:
                notes.extend(f"clip-{clip.index:02d}/provider: {note}" for note in payload.notes)
        except Exception as exc:
            diagnostics.append(f"clip-{clip.index:02d} failed: {exc}")

    deduped = _dedupe_seeds(seeds)
    status = "provider_analyzed" if successful_clips > 0 else "provider_failed"
    if successful_clips > 0 and not deduped:
        notes.append("RTSP live YOLO + Provider completed successfully and returned no hazards.")

    return _result(
        status,
        deduped,
        diagnostics,
        notes,
        clip_count=len(prepared_clips),
        successful_clips=successful_clips,
        selected_model=selected_model,
        rtsp_analysis_mode="live",
    )


def run_provider_yolo_analysis(
    project: Project,
    rules: list[HazardRule],
    model_override: str | None = None,
    *,
    video_path: Path | None = None,
    video_start_ts: int | None = None,
    video_end_ts: int | None = None,
    video_label: str = "primary",
) -> AnalysisRunResult:
    """Run YOLO on each prepared video clip, then LLM review with detection context."""
    diagnostics: list[str] = []
    notes: list[str] = []
    selected_model = resolve_vision_model(model_override)

    validation_error = _validate_provider_yolo_prerequisites(selected_model)
    if validation_error is not None:
        status, diagnostics = validation_error
        return _result(status, [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)
    _append_vision_provider_warning(notes, selected_model)

    resolved_video_path = video_path or resolve_project_path(
        project.artifacts_dir or "",
        project.inspection_video_path,
        "artifacts/inspection.mp4",
    )
    if not resolved_video_path.exists():
        diagnostics.append(f"Project video not found: {resolved_video_path}")
        return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)

    if video_start_ts is not None and video_end_ts is not None:
        resolved_video_start_ts = video_start_ts
        resolved_video_end_ts = video_end_ts
    else:
        dataset_summary_path = Path(project.artifacts_dir) / "summaries" / "dataset_summary.json"
        if not dataset_summary_path.exists():
            diagnostics.append("Dataset summary is missing")
            return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)
        dataset_summary = read_json(dataset_summary_path)
        resolved_video_start_ts = int(dataset_summary.get("video_start_ts") or project.bag_start_ts or 0)
        resolved_video_end_ts = int(dataset_summary.get("video_end_ts") or project.bag_end_ts or resolved_video_start_ts)

    if resolved_video_end_ts <= resolved_video_start_ts:
        diagnostics.append("Video timestamps are invalid; cannot prepare provider clips")
        return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)

    clip_dir = Path(project.artifacts_dir) / "artifacts" / "analysis_clips" / video_label
    yolo_dir = Path(project.artifacts_dir) / "artifacts" / "yolo_results" / video_label
    clip_dir.mkdir(parents=True, exist_ok=True)
    yolo_dir.mkdir(parents=True, exist_ok=True)
    remove_paths(list(clip_dir.glob("clip_*.mp4")))
    remove_paths(list(yolo_dir.glob("clip_*.json")))

    prepared_clips, clip_diagnostics = _prepare_clips(
        video_path=resolved_video_path,
        clip_dir=clip_dir,
        video_start_ts=resolved_video_start_ts,
        video_end_ts=resolved_video_end_ts,
    )
    diagnostics.extend(clip_diagnostics)
    if not prepared_clips:
        diagnostics.append("No provider clips were prepared successfully")
        return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)

    if video_label != "primary":
        notes.append(f"Analyzing RTSP video target `{video_label}` at {resolved_video_path.name}.")

    rules_by_id = {rule.rule_id: rule for rule in rules}
    seeds: list[FindingSeed] = []
    successful_clips = 0

    for clip in prepared_clips:
        try:
            if RULE_RAG_ENABLED:
                clip_seeds, yolo_result, payload, yolo_prompt_section, retrieved_payloads = (
                    _analyze_clip_with_yolo_rag_then_provider(
                        clip=clip,
                        rules=rules,
                        rules_by_id=rules_by_id,
                        selected_model=selected_model,
                    )
                )
                _persist_yolo_result(
                    yolo_dir,
                    clip,
                    yolo_result,
                    yolo_prompt_section,
                    retrieved_payloads=retrieved_payloads,
                )
            else:
                clip_seeds, yolo_result, payload, yolo_prompt_section = _analyze_clip_with_yolo_then_provider(
                    clip=clip,
                    rules=rules,
                    rules_by_id=rules_by_id,
                    selected_model=selected_model,
                )
                _persist_yolo_result(yolo_dir, clip, yolo_result, yolo_prompt_section)
            seeds.extend(clip_seeds)
            successful_clips += 1
            if yolo_result.notes:
                notes.extend(f"clip-{clip.index:02d}/yolo: {note}" for note in yolo_result.notes)
            notes.append(
                f"clip-{clip.index:02d}: YOLO found {len(yolo_result.detections)} detections; "
                f"{selected_model} returned {len(clip_seeds)} findings."
            )
            if payload.notes:
                notes.extend(f"clip-{clip.index:02d}/provider: {note}" for note in payload.notes)
        except Exception as exc:
            diagnostics.append(f"clip-{clip.index:02d} failed: {exc}")

    deduped = _dedupe_seeds(seeds)
    status = "provider_analyzed" if successful_clips > 0 else "provider_failed"
    if successful_clips > 0 and not deduped:
        notes.append("YOLO + Provider completed successfully and returned no hazards.")

    return _result(
        status,
        deduped,
        diagnostics,
        notes,
        clip_count=len(prepared_clips),
        successful_clips=successful_clips,
        selected_model=selected_model,
        rtsp_analysis_mode="recording",
    )


def _validate_provider_yolo_prerequisites(
    selected_model: str,
) -> tuple[str, list[str]] | None:
    diagnostics: list[str] = []
    if not YOLO_API_URL:
        diagnostics.append("YOLO_API_URL is not configured")
        return "provider_failed", diagnostics
    return None


def _append_vision_provider_warning(notes: list[str], selected_model: str) -> None:
    if provider_available(selected_model):
        return
    notes.append(
        "Vision provider is not configured; YOLO will still run and provider steps may fail."
    )


def _resolve_rtsp_live_duration_sec(project: Project) -> float:
    dataset_summary_path = Path(project.artifacts_dir) / "summaries" / "dataset_summary.json"
    if dataset_summary_path.exists():
        dataset_summary = read_json(dataset_summary_path)
        video_start_ts = int(dataset_summary.get("video_start_ts") or project.bag_start_ts or 0)
        video_end_ts = int(dataset_summary.get("video_end_ts") or project.bag_end_ts or video_start_ts)
        if video_end_ts > video_start_ts:
            duration_sec = (video_end_ts - video_start_ts) / 1000.0
            return min(max(duration_sec, 1.0), YOLO_RTSP_MAX_DURATION_SEC)

    if project.bag_start_ts and project.bag_end_ts and project.bag_end_ts > project.bag_start_ts:
        duration_sec = (project.bag_end_ts - project.bag_start_ts) / 1000.0
        return min(max(duration_sec, 1.0), YOLO_RTSP_MAX_DURATION_SEC)

    return min(max(YOLO_RTSP_DEFAULT_DURATION_SEC, 1.0), YOLO_RTSP_MAX_DURATION_SEC)


def _analyze_clip_with_yolo_then_provider(
    *,
    clip: PreparedClip,
    rules: list[HazardRule],
    rules_by_id: dict[str, HazardRule],
    selected_model: str,
    yolo_result: YoloClipResult | None = None,
) -> tuple[list[FindingSeed], YoloClipResult, ProviderResponsePayload, str]:
    """Run YOLO on one clip, append detections to the prompt, then call the LLM with video+text."""
    resolved_yolo_result = yolo_result or _invoke_yolo(clip=clip)
    yolo_prompt_section = _build_yolo_prompt_section(clip=clip, rules=rules, yolo_result=resolved_yolo_result)
    payload = _invoke_dashscope(
        clip=clip,
        rules=rules,
        selected_model=selected_model,
        extra_context=yolo_prompt_section,
    )
    clip_seeds = _clip_findings_to_seeds(clip, payload, rules_by_id)
    return clip_seeds, resolved_yolo_result, payload, yolo_prompt_section


def _analyze_clip_with_yolo_rag_then_provider(
    *,
    clip: PreparedClip,
    rules: list[HazardRule],
    rules_by_id: dict[str, HazardRule],
    selected_model: str,
    yolo_result: YoloClipResult | None = None,
) -> tuple[list[FindingSeed], YoloClipResult, ProviderResponsePayload, str, list[dict[str, object]]]:
    """YOLO -> rule DB retrieval -> prompt with retrieved rules -> LLM."""
    resolved_yolo_result = yolo_result or _invoke_yolo(clip=clip)
    yolo_detections = [item.model_dump() for item in resolved_yolo_result.detections]

    project_id = rules[0].project_id if rules else 0
    retrieved_payloads = retrieve_rules_for_clip(
        yolo_detections=yolo_detections,
        checker_scope=_infer_checker_scope(rules),
        domain="industrial-inspection",
    )
    retrieved_rules = hazard_rules_from_payloads(retrieved_payloads, project_id=project_id or 0)
    if not retrieved_rules:
        retrieved_rules = rules
        retrieved_payloads = _fallback_rule_payloads(rules)

    yolo_prompt_section = _build_yolo_prompt_section(
        clip=clip,
        rules=retrieved_rules,
        yolo_result=resolved_yolo_result,
        retrieved_payloads=retrieved_payloads,
    )
    payload = _invoke_dashscope(
        clip=clip,
        rules=retrieved_rules,
        selected_model=selected_model,
        extra_context=yolo_prompt_section,
        retrieved_rules=True,
    )
    effective_rules_by_id = dict(rules_by_id)
    effective_rules_by_id.update({rule.rule_id: rule for rule in retrieved_rules})
    clip_seeds = _clip_findings_to_seeds(clip, payload, effective_rules_by_id)
    return clip_seeds, resolved_yolo_result, payload, yolo_prompt_section, retrieved_payloads


def _fallback_rule_payloads(rules: list[HazardRule]) -> list[dict[str, object]]:
    payloads = export_rules_payload(rules)
    for payload in payloads:
        payload["_retrieval"] = {
            "score": 0.0,
            "matched_classes": [],
            "matched_reasons": ["fallback=current project visual rules"],
        }
    return payloads


def _infer_checker_scope(rules: list[HazardRule]) -> str | None:
    scopes = [rule.checker_scope for rule in rules if rule.checker_scope and rule.checker_scope != "mixed"]
    if not scopes:
        return None
    counts: dict[str, int] = {}
    for scope in scopes:
        counts[scope] = counts.get(scope, 0) + 1
    return max(counts, key=counts.get)


def _build_yolo_prompt_section(
    *,
    clip: PreparedClip,
    rules: list[HazardRule],
    yolo_result: YoloClipResult,
    retrieved_payloads: list[dict[str, object]] | None = None,
) -> str:
    """Format YOLO detections into supplemental prompt text for the multimodal LLM."""
    lines = [
        "=== YOLO pre-analysis context ===",
        "The same video clip was already processed by an on-premise YOLO detector.",
        f"Clip-relative time range: 0.0s to {clip.duration_sec:.1f}s.",
        f"Total detections above threshold: {len(yolo_result.detections)}.",
        "",
        "Detected objects:",
    ]

    if not yolo_result.detections:
        lines.append("- (none)")
    else:
        for detection in yolo_result.detections[:40]:
            offset_sec = _clip_relative_time_sec(clip, detection.time_sec)
            parts = [
                f"class={detection.class_name}",
                f"confidence={detection.confidence:.2f}",
            ]
            if offset_sec is not None:
                parts.append(f"clip_offset_sec={offset_sec:.2f}")
            if detection.bbox:
                bbox_text = ", ".join(f"{value:.1f}" for value in detection.bbox[:4])
                parts.append(f"bbox=[{bbox_text}]")
            lines.append("- " + ", ".join(parts))

        if len(yolo_result.detections) > 40:
            lines.append(f"- ... and {len(yolo_result.detections) - 40} more detections omitted")

    rule_hints = (
        _retrieval_rule_hints(retrieved_payloads)
        if retrieved_payloads is not None
        else _match_yolo_detections_to_rules(yolo_result.detections, rules)
    )
    lines.extend(["", "Possible rule correlations suggested by rule retrieval:"])
    if rule_hints:
        lines.extend(rule_hints)
    else:
        lines.append("- No direct class-to-rule mapping was inferred; inspect the video visually.")

    if yolo_result.notes:
        lines.extend(["", "YOLO service notes:"])
        lines.extend(f"- {note}" for note in yolo_result.notes)

    lines.extend(
        [
            "",
            "How to use this YOLO context:",
            "- YOLO detections are strong candidate evidence for this clip; use them with the video.",
            "- When rule correlations are listed above, actively evaluate those rule_id values.",
            "- Report a finding when YOLO and/or the video support the hazard — "
            "do not require perfect clarity if the object class and scene are consistent.",
            "- Prefer clip-relative timestamps that align with detection times or visible evidence.",
            "- Ignore YOLO classes that cannot map to any listed inspection rule.",
            "=== End YOLO context ===",
        ]
    )
    return "\n".join(lines)


def _retrieval_rule_hints(retrieved_payloads: list[dict[str, object]]) -> list[str]:
    hints: list[str] = []
    for payload in retrieved_payloads:
        rule_id = payload.get("ruleId") or payload.get("rule_id")
        hazard_desc = str(payload.get("hazardDesc") or payload.get("hazard_desc") or "")
        retrieval = payload.get("_retrieval")
        if not isinstance(retrieval, dict):
            continue
        matched_classes = retrieval.get("matched_classes") or []
        matched_reasons = retrieval.get("matched_reasons") or []
        if not matched_classes:
            continue
        hints.append(
            f"- YOLO classes {matched_classes} may relate to rule `{rule_id}` "
            f"({hazard_desc[:60]}{'...' if len(hazard_desc) > 60 else ''})"
        )
        if matched_reasons:
            hints.append(f"  retrieval reasons: {matched_reasons}")
    return hints


def _clip_relative_time_sec(clip: PreparedClip, time_sec: float | None) -> float | None:
    if time_sec is None:
        return None
    # Clips prepared from a mid-recording window use absolute recording times.
    # Clips that start at 0 already carry clip-local times.
    if clip.start_offset_sec > 1e-6:
        relative = time_sec - clip.start_offset_sec
    else:
        relative = time_sec
    return max(0.0, min(clip.duration_sec, relative))


def _recording_from_active_watchdog(
    *,
    rtsp_url: str,
    capture_path: Path,
    active,
    start_offset_sec: float,
    duration_sec: float,
    analysis_started_ms: int,
):
    """Cut the analysis window from the in-progress watchdog recording (do not wait for EOF)."""
    import time

    from .rtsp_recorder import _finalize_recording_result

    active_path = None if active is None else getattr(active, "output_path", None)
    if active is not None and (active_path is None or not Path(active_path).is_file() or Path(active_path).stat().st_size <= 0):
        # Recording just started — wait for the file to appear, never for stream EOF.
        deadline = time.monotonic() + max(15.0, min(60.0, duration_sec + 10.0))
        while time.monotonic() < deadline:
            candidate = getattr(active, "output_path", None)
            if candidate is not None and Path(candidate).is_file() and Path(candidate).stat().st_size > 0:
                active_path = Path(candidate)
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Active watchdog recording has not produced a readable file yet.")

    if active_path is None:
        recording_path = wait_for_completed_recording(
            rtsp_url,
            timeout_sec=max(duration_sec + 30.0, float(YOLO_REQUEST_TIMEOUT_SECONDS)),
        )
        return recording_result_from_path(rtsp_url=rtsp_url, output_path=recording_path)

    active_path = Path(active_path)
    needed_sec = max(0.0, float(start_offset_sec)) + float(duration_sec)
    last_error: Exception | None = None
    for _ in range(12):
        try:
            probed_duration, _ = probe_recorded_video(active_path)
            if probed_duration > 0 and probed_duration + 0.75 < needed_sec:
                time.sleep(0.75)
                continue
            extract_video_clip(
                source_path=active_path,
                output_path=capture_path,
                start_sec=start_offset_sec,
                duration_sec=duration_sec,
            )
            return _finalize_recording_result(
                rtsp_url=rtsp_url,
                output_path=capture_path,
                video_start_ts=analysis_started_ms,
                fallback_duration_sec=duration_sec,
            )
        except Exception as exc:
            last_error = exc
            time.sleep(0.75)

    raise RuntimeError(
        f"Failed to cut analysis window from active watchdog recording: {last_error}"
    )


def _match_yolo_detections_to_rules(
    detections: list[YoloDetectionPayload],
    rules: list[HazardRule],
) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()

    for detection in detections:
        matched_rules = _rules_for_yolo_class(detection.class_name, rules)
        for rule in matched_rules:
            key = f"{rule.rule_id}:{detection.class_name}"
            if key in seen:
                continue
            seen.add(key)
            hints.append(
                f"- YOLO class `{detection.class_name}` may relate to rule `{rule.rule_id}` "
                f"({rule.hazard_desc[:60]}{'...' if len(rule.hazard_desc) > 60 else ''})"
            )
    return hints


def _rules_for_yolo_class(class_name: str, rules: list[HazardRule]) -> list[HazardRule]:
    class_tokens = _yolo_class_match_tokens(class_name)
    if not class_tokens:
        return []

    matched: list[HazardRule] = []
    for rule in rules:
        candidates = _rule_match_tokens(rule)
        if any(
            token == candidate or token in candidate or candidate in token
            for token in class_tokens
            for candidate in candidates
            if candidate
        ):
            matched.append(rule)
    return matched


def _yolo_class_match_tokens(class_name: str) -> set[str]:
    """Expand a YOLO class into match tokens via object aliases (e.g. dust → 粉尘)."""
    from .object_alias import load_alias_map, normalize_object_name

    raw = (class_name or "").strip()
    if not raw:
        return set()

    alias_map = load_alias_map()
    canonical = normalize_object_name(raw, alias_map)
    tokens = {
        _normalize_match_text(raw),
        _normalize_match_text(canonical),
    }
    canonical_key = canonical.strip().lower()
    for alias, mapped in alias_map.items():
        if mapped.strip().lower() == canonical_key or alias == raw.lower():
            tokens.add(_normalize_match_text(alias))
            tokens.add(_normalize_match_text(mapped))
    return {token for token in tokens if token}


def _rule_match_tokens(rule: HazardRule) -> set[str]:
    from .object_alias import load_alias_map, normalize_object_name

    alias_map = load_alias_map()
    raw_values = [rule.object_name, rule.check_item]
    try:
        evidence_objects = json.loads(rule.evidence_objects_json)
    except json.JSONDecodeError:
        evidence_objects = []
    if isinstance(evidence_objects, list):
        raw_values.extend(str(item) for item in evidence_objects)

    tokens: set[str] = set()
    for value in raw_values:
        text = (value or "").strip()
        if not text:
            continue
        tokens.add(_normalize_match_text(text))
        tokens.add(_normalize_match_text(normalize_object_name(text, alias_map)))
    return {token for token in tokens if token}


def _normalize_match_text(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip()).strip("_")


def _split_yolo_result_for_clip(full_result: YoloClipResult, clip: PreparedClip) -> YoloClipResult:
    window_start = clip.start_offset_sec
    window_end = clip.start_offset_sec + clip.duration_sec
    clip_detections: list[YoloDetectionPayload] = []

    for detection in full_result.detections:
        if detection.time_sec is None:
            continue
        if detection.time_sec < window_start or detection.time_sec >= window_end:
            continue
        clip_detections.append(
            YoloDetectionPayload(
                class_name=detection.class_name,
                confidence=detection.confidence,
                time_sec=detection.time_sec - window_start,
                bbox=list(detection.bbox),
            )
        )

    return YoloClipResult(
        detections=clip_detections,
        notes=list(full_result.notes),
        raw_payload=dict(full_result.raw_payload),
    )


def _invoke_yolo_rtsp(
    *,
    rtsp_url: str,
    duration_sec: float,
    clip_index: str,
    rtsp_transport: str,
    segment_index: int = 0,
    segment_start_sec: float = 0.0,
) -> YoloClipResult:
    endpoint = _build_yolo_rtsp_endpoint()
    client_rtsp_url = resolve_yolo_client_rtsp_url(rtsp_url)
    payload = {
        "rtsp_url": client_rtsp_url,
        "duration_sec": duration_sec,
        "clip_index": clip_index,
        "rtsp_transport": rtsp_transport,
        "segment_index": segment_index,
        "segment_start_sec": segment_start_sec,
    }
    request_timeout = max(YOLO_REQUEST_TIMEOUT_SECONDS, int(duration_sec) + 30)
    last_error: Exception | None = None
    max_attempts = max(1, YOLO_MAX_RETRIES + 1)

    for attempt in range(1, max_attempts + 1):
        try:
            headers = {"Content-Type": "application/json"}
            if YOLO_API_KEY:
                headers["Authorization"] = f"Bearer {YOLO_API_KEY}"

            req = request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with request.urlopen(req, timeout=request_timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            return _parse_yolo_payload(response_payload)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"YOLO RTSP HTTP {exc.code}: {detail or exc.reason}")
            if exc.code in {408, 409, 429, 500, 502, 503, 504} and attempt < max_attempts:
                continue
            break
        except (error.URLError, TimeoutError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < max_attempts:
                continue
            break

    if YOLO_FAIL_OPEN:
        return YoloClipResult(
            detections=[],
            notes=[f"YOLO RTSP request failed and fail-open is enabled: {last_error}"],
            raw_payload={},
        )
    raise RuntimeError(str(last_error) if last_error else "YOLO RTSP request failed")


def invoke_yolo_rtsp_segment(
    *,
    rtsp_url: str,
    duration_sec: float,
    clip_index: str,
    rtsp_transport: str,
    segment_index: int = 0,
    segment_start_sec: float = 0.0,
) -> YoloClipResult:
    """Public wrapper for one YOLO /predict/rtsp segment (used by the RTSP watchdog monitor)."""
    return _invoke_yolo_rtsp(
        rtsp_url=rtsp_url,
        duration_sec=duration_sec,
        clip_index=clip_index,
        rtsp_transport=rtsp_transport,
        segment_index=segment_index,
        segment_start_sec=segment_start_sec,
    )


def _invoke_yolo_rtsp_segments(
    *,
    rtsp_url: str,
    total_duration_sec: float,
    rtsp_transport: str,
    output_dir: Path | None = None,
) -> YoloClipResult:
    """Call YOLO once per RTSP segment and merge detections onto a shared timeline.

    YOLO RTSP samples the live edge for each segment's wall duration. After each call
    we label the segment as the trailing ``segment_duration`` window so detection times
    align with the extracted watchdog/live capture.
    """
    import time

    segment_seconds = max(1.0, YOLO_RTSP_SEGMENT_SECONDS)
    segment_results: list[tuple[int, float, float, YoloClipResult]] = []
    requested_video_sec = 0.0
    segment_index = 0
    analysis_started = time.monotonic()

    while requested_video_sec < total_duration_sec - 1e-6:
        segment_duration = min(segment_seconds, total_duration_sec - requested_video_sec)
        segment_result = _invoke_yolo_rtsp(
            rtsp_url=rtsp_url,
            duration_sec=segment_duration,
            clip_index=f"rtsp_live_{segment_index:02d}",
            rtsp_transport=rtsp_transport,
            segment_index=segment_index,
            segment_start_sec=requested_video_sec,
        )
        segment_end = max(segment_duration, time.monotonic() - analysis_started)
        segment_start = max(0.0, segment_end - segment_duration)
        if output_dir is not None:
            _persist_yolo_segment_result(
                output_dir,
                segment_index=segment_index,
                segment_start_sec=segment_start,
                segment_duration_sec=segment_duration,
                yolo_result=segment_result,
            )
        segment_results.append((segment_index, segment_start, segment_duration, segment_result))
        requested_video_sec += segment_duration
        segment_index += 1

    return _merge_yolo_segment_results(segment_results)


def _merge_yolo_segment_results(
    segment_results: list[tuple[int, float, float, YoloClipResult]],
) -> YoloClipResult:
    merged_detections: list[YoloDetectionPayload] = []
    merged_notes: list[str] = []
    raw_segments: list[dict[str, object]] = []

    for segment_index, segment_start, segment_duration, segment_result in segment_results:
        merged_notes.append(
            f"segment-{segment_index:02d}: start={segment_start:.1f}s "
            f"duration={segment_duration:.1f}s detections={len(segment_result.detections)}"
        )
        merged_notes.extend(
            f"segment-{segment_index:02d}/{note}"
            for note in segment_result.notes
            if note not in merged_notes
        )
        for detection in segment_result.detections:
            global_time_sec = segment_start + (detection.time_sec or 0.0)
            merged_detections.append(
                YoloDetectionPayload(
                    class_name=detection.class_name,
                    confidence=detection.confidence,
                    time_sec=global_time_sec,
                    bbox=list(detection.bbox),
                )
            )
        raw_segments.append(
            {
                "segment_index": segment_index,
                "segment_start_sec": segment_start,
                "segment_duration_sec": segment_duration,
                "detection_count": len(segment_result.detections),
                "raw": segment_result.raw_payload,
            }
        )

    return YoloClipResult(
        detections=merged_detections,
        notes=merged_notes,
        raw_payload={
            "segment_count": len(segment_results),
            "segments": raw_segments,
        },
    )


def _persist_yolo_segment_result(
    output_dir: Path,
    *,
    segment_index: int,
    segment_start_sec: float,
    segment_duration_sec: float,
    yolo_result: YoloClipResult,
) -> None:
    output_path = output_dir / f"segment_{segment_index:03d}.json"
    output_path.write_text(
        json.dumps(
            {
                "segment_index": segment_index,
                "segment_start_sec": segment_start_sec,
                "segment_duration_sec": segment_duration_sec,
                "detections": [item.model_dump() for item in yolo_result.detections],
                "notes": yolo_result.notes,
                "raw": yolo_result.raw_payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _invoke_yolo(*, clip: PreparedClip) -> YoloClipResult:
    endpoint = _build_yolo_endpoint()
    video_bytes = clip.path.read_bytes()
    last_error: Exception | None = None
    max_attempts = max(1, YOLO_MAX_RETRIES + 1)

    for attempt in range(1, max_attempts + 1):
        try:
            body, content_type = _encode_multipart(
                fields={"clip_index": str(clip.index)},
                files={"file": (clip.path.name, video_bytes, "video/mp4")},
            )
            headers = {"Content-Type": content_type}
            if YOLO_API_KEY:
                headers["Authorization"] = f"Bearer {YOLO_API_KEY}"

            req = request.Request(endpoint, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=YOLO_REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return _parse_yolo_payload(payload)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"YOLO HTTP {exc.code}: {detail or exc.reason}")
            if exc.code in {408, 409, 429, 500, 502, 503, 504} and attempt < max_attempts:
                continue
            break
        except (error.URLError, TimeoutError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < max_attempts:
                continue
            break

    if YOLO_FAIL_OPEN:
        return YoloClipResult(
            detections=[],
            notes=[f"YOLO request failed and fail-open is enabled: {last_error}"],
            raw_payload={},
        )
    raise RuntimeError(str(last_error) if last_error else "YOLO request failed")


def _build_yolo_endpoint() -> str:
    base = YOLO_API_URL.rstrip("/")
    path = YOLO_DETECT_PATH if YOLO_DETECT_PATH.startswith("/") else f"/{YOLO_DETECT_PATH}"
    return f"{base}{path}"


def _build_yolo_rtsp_endpoint() -> str:
    base = YOLO_API_URL.rstrip("/")
    path = YOLO_RTSP_DETECT_PATH if YOLO_RTSP_DETECT_PATH.startswith("/") else f"/{YOLO_RTSP_DETECT_PATH}"
    return f"{base}{path}"


def _encode_multipart(
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----YoloBoundary{secrets.token_hex(16)}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8"),
                content,
                b"\r\n",
            ]
        )

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _parse_yolo_payload(payload: object) -> YoloClipResult:
    if not isinstance(payload, dict):
        raise RuntimeError("YOLO response root must be a JSON object")

    notes: list[str] = []
    raw_notes = payload.get("notes")
    if isinstance(raw_notes, list):
        notes.extend(str(item) for item in raw_notes if str(item).strip())

    detections: list[YoloDetectionPayload] = []
    if isinstance(payload.get("detections"), list):
        detections.extend(_normalize_detection_items(payload["detections"]))
    elif isinstance(payload.get("frames"), list):
        for frame in payload["frames"]:
            if not isinstance(frame, dict):
                continue
            frame_time = _coerce_float(frame.get("timestamp_sec") or frame.get("time_sec"))
            frame_items = frame.get("detections") or frame.get("objects") or []
            if isinstance(frame_items, list):
                detections.extend(_normalize_detection_items(frame_items, default_time_sec=frame_time))
    elif isinstance(payload.get("results"), list):
        detections.extend(_normalize_detection_items(payload["results"]))
    else:
        for key in ("predictions", "objects"):
            items = payload.get(key)
            if isinstance(items, list):
                detections.extend(_normalize_detection_items(items))
                break

    filtered = [
        item
        for item in detections
        if item.confidence >= YOLO_CONFIDENCE_THRESHOLD and item.class_name.strip()
    ]
    return YoloClipResult(detections=filtered, notes=notes, raw_payload=payload)


def _normalize_detection_items(items: object, *, default_time_sec: float | None = None) -> list[YoloDetectionPayload]:
    if not isinstance(items, list):
        return []

    normalized: list[YoloDetectionPayload] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        class_name = str(
            item.get("class_name")
            or item.get("class")
            or item.get("label")
            or item.get("name")
            or ""
        ).strip()
        confidence = _coerce_float(item.get("confidence") or item.get("score") or item.get("prob")) or 0.0
        time_sec = _coerce_float(item.get("time_sec") or item.get("timestamp_sec") or item.get("timestamp"))
        if time_sec is None:
            time_sec = default_time_sec

        bbox_raw = item.get("bbox") or item.get("box") or item.get("xyxy")
        bbox: list[float] = []
        if isinstance(bbox_raw, list):
            bbox = [float(value) for value in bbox_raw[:4]]
        elif isinstance(bbox_raw, dict):
            for key in ("x1", "y1", "x2", "y2"):
                value = bbox_raw.get(key)
                if value is not None:
                    bbox.append(float(value))

        normalized.append(
            YoloDetectionPayload(
                class_name=class_name,
                confidence=max(0.0, min(1.0, confidence)),
                time_sec=time_sec,
                bbox=bbox,
            )
        )
    return normalized


def _persist_yolo_result(
    output_dir: Path,
    clip: PreparedClip,
    yolo_result: YoloClipResult,
    provider_prompt_section: str,
    *,
    retrieved_payloads: list[dict[str, object]] | None = None,
) -> None:
    output_path = output_dir / f"clip_{clip.index:03d}.json"

    if retrieved_payloads is not None:
        save_retrieval_artifact(
            output_path=output_path,
            clip_id=f"clip_{clip.index:03d}",
            yolo_detections=[item.model_dump() for item in yolo_result.detections],
            retrieved_rules=retrieved_payloads,
            provider_prompt_section=provider_prompt_section,
        )
        return

    payload = YoloClipResponsePayload(
        detections=yolo_result.detections,
        notes=yolo_result.notes,
    )
    output_path.write_text(
        json.dumps(
            {
                "clip_index": clip.index,
                "clip_start_offset_sec": clip.start_offset_sec,
                "clip_duration_sec": clip.duration_sec,
                "detections": [item.model_dump() for item in payload.detections],
                "notes": payload.notes,
                "provider_prompt_section": provider_prompt_section,
                "raw": yolo_result.raw_payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _result(
    status: str,
    findings: list[FindingSeed],
    diagnostics: list[str],
    notes: list[str],
    *,
    clip_count: int,
    successful_clips: int,
    selected_model: str,
    rtsp_analysis_mode: str | None = None,
) -> AnalysisRunResult:
    unique_notes = list(dict.fromkeys(note for note in notes if note))
    unique_diagnostics = list(dict.fromkeys(diag for diag in diagnostics if diag))
    summary = {
        "analysis_mode": "provider_yolo",
        "analysis_provider": provider_yolo_label(),
        "analysis_model": selected_model,
        "provider_available": yolo_available(),
        "yolo_available": yolo_available(),
        "vision_provider_available": provider_available(selected_model),
        "yolo_api_url": YOLO_API_URL,
        "yolo_confidence_threshold": YOLO_CONFIDENCE_THRESHOLD,
        "yolo_rtsp_segment_seconds": YOLO_RTSP_SEGMENT_SECONDS,
        "rule_rag_enabled": RULE_RAG_ENABLED,
        "status": status,
        "clip_count": clip_count,
        "successful_clips": successful_clips,
        "failed_clips": max(0, clip_count - successful_clips),
        "notes": unique_notes,
        "diagnostics": unique_diagnostics,
    }
    if rtsp_analysis_mode:
        summary["rtsp_analysis_mode"] = rtsp_analysis_mode
    return AnalysisRunResult(status=status, findings=findings, summary=summary)


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
