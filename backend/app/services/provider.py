from __future__ import annotations

import base64
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

from pydantic import BaseModel, Field, ValidationError

from ..models import HazardRule, Project
from ..settings import (
    CLIP_COMPRESS_TIMEOUT_SECONDS,
    FFMPEG_BIN,
    VISION_API_KEY,
    VISION_API_URL,
    VISION_CLIP_SECONDS,
    VISION_ENABLE_THINKING,
    VISION_MAX_CLIP_BYTES,
    VISION_MAX_FINDINGS_PER_CLIP,
    VISION_MAX_RETRIES,
    VISION_REQUEST_TIMEOUT_SECONDS,
    VISION_TEMPERATURE,
    VISION_VIDEO_FPS,
    VISION_PROVIDER,
    is_supported_vision_model,
    resolve_vision_model,
)
from .analysis_types import AnalysisRunResult, FindingSeed
from .llm_output_log import write_llm_log
from .storage import read_json, remove_paths, resolve_project_path


@dataclass
class PreparedClip:
    index: int
    path: Path
    start_offset_sec: float
    duration_sec: float
    start_ts_ms: int
    byte_size: int
    profile_name: str


class ProviderFindingPayload(BaseModel):
    rule_id: str = Field(min_length=1)
    start_offset_sec: float = 0.0
    end_offset_sec: float = 0.0
    evidence_sec: list[float] = Field(default_factory=list)
    description: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ProviderResponsePayload(BaseModel):
    findings: list[ProviderFindingPayload] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)



def provider_available(model_override: str | None = None) -> bool:
    selected_model = resolve_vision_model(model_override)
    return VISION_PROVIDER == "dashscope" and bool(VISION_API_KEY and VISION_API_URL and selected_model)



def provider_label() -> str:
    return VISION_PROVIDER or "unconfigured"



def run_provider_analysis(
    project: Project,
    rules: list[HazardRule],
    model_override: str | None = None,
    *,
    video_path: Path | None = None,
    video_start_ts: int | None = None,
    video_end_ts: int | None = None,
    video_label: str = "primary",
) -> AnalysisRunResult:
    diagnostics: list[str] = []
    notes: list[str] = []
    selected_model = resolve_vision_model(model_override)

    if VISION_PROVIDER != "dashscope":
        diagnostics.append(f"Unsupported provider: {VISION_PROVIDER or 'unset'}")
        return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)
    if not VISION_API_KEY:
        diagnostics.append("VISION_API_KEY is not configured")
        return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)
    if not is_supported_vision_model(selected_model):
        diagnostics.append(f"Model is not in the configured supported list: {selected_model}")
        return _result("provider_failed", [], diagnostics, notes, clip_count=0, successful_clips=0, selected_model=selected_model)

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
    clip_dir.mkdir(parents=True, exist_ok=True)
    remove_paths(list(clip_dir.glob("clip_*.mp4")))

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
            payload = _invoke_dashscope(clip=clip, rules=rules, selected_model=selected_model)
            clip_seeds = _clip_findings_to_seeds(clip, payload, rules_by_id)
            seeds.extend(clip_seeds)
            successful_clips += 1
            if payload.notes:
                notes.extend(f"clip-{clip.index:02d}: {note}" for note in payload.notes)
            notes.append(
                f"clip-{clip.index:02d} analyzed by {selected_model} with {clip.profile_name} profile ({clip.byte_size / 1024:.1f} KB, {len(clip_seeds)} findings)."
            )
        except Exception as exc:
            diagnostics.append(f"clip-{clip.index:02d} failed: {exc}")

    deduped = _dedupe_seeds(seeds)
    status = "provider_analyzed" if successful_clips > 0 else "provider_failed"
    if successful_clips > 0 and not deduped:
        notes.append("Provider completed successfully and returned no hazards.")

    return _result(
        status,
        deduped,
        diagnostics,
        notes,
        clip_count=len(prepared_clips),
        successful_clips=successful_clips,
        selected_model=selected_model,
    )



def _prepare_clips(
    *,
    video_path: Path,
    clip_dir: Path,
    video_start_ts: int,
    video_end_ts: int,
) -> tuple[list[PreparedClip], list[str]]:
    diagnostics: list[str] = []
    clips: list[PreparedClip] = []
    duration_sec = max(1.0, (video_end_ts - video_start_ts) / 1000.0)
    clip_count = max(1, math.ceil(duration_sec / VISION_CLIP_SECONDS))

    for index in range(clip_count):
        start_offset_sec = index * VISION_CLIP_SECONDS
        remaining_sec = duration_sec - start_offset_sec
        clip_duration_sec = min(VISION_CLIP_SECONDS, remaining_sec)
        if clip_duration_sec <= 0:
            continue
        clip, clip_error = _compress_clip(
            video_path=video_path,
            clip_dir=clip_dir,
            index=index,
            start_offset_sec=start_offset_sec,
            duration_sec=clip_duration_sec,
            start_ts_ms=video_start_ts + int(round(start_offset_sec * 1000)),
        )
        if clip is not None:
            clips.append(clip)
        elif clip_error:
            diagnostics.append(clip_error)
    return clips, diagnostics



def _compress_clip(
    *,
    video_path: Path,
    clip_dir: Path,
    index: int,
    start_offset_sec: float,
    duration_sec: float,
    start_ts_ms: int,
) -> tuple[PreparedClip | None, str | None]:
    profiles = [
        {"name": "wide-720p", "width": 960, "height": 720, "crf": 30, "fps": VISION_VIDEO_FPS},
        {"name": "balanced-540p", "width": 720, "height": 540, "crf": 34, "fps": min(VISION_VIDEO_FPS, 1.0)},
        {"name": "compact-480p", "width": 640, "height": 480, "crf": 38, "fps": min(VISION_VIDEO_FPS, 1.0)},
    ]
    last_error: str | None = None

    for profile in profiles:
        output_path = clip_dir / f"clip_{index:03d}_{profile['name']}.mp4"
        remove_paths([output_path])
        command = [
            FFMPEG_BIN,
            "-y",
            "-ss",
            f"{start_offset_sec:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration_sec:.3f}",
            "-an",
            "-vf",
            f"fps={max(0.1, float(profile['fps']))},scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=decrease",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(profile["crf"]),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=CLIP_COMPRESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            remove_paths([output_path])
            last_error = f"ffmpeg clip compression timed out after {CLIP_COMPRESS_TIMEOUT_SECONDS}s"
            continue
        if result.returncode != 0:
            remove_paths([output_path])
            last_error = result.stderr.strip() or "ffmpeg clip preparation failed"
            continue

        size_bytes = output_path.stat().st_size
        if size_bytes <= VISION_MAX_CLIP_BYTES:
            return (
                PreparedClip(
                    index=index,
                    path=output_path,
                    start_offset_sec=start_offset_sec,
                    duration_sec=duration_sec,
                    start_ts_ms=start_ts_ms,
                    byte_size=size_bytes,
                    profile_name=str(profile["name"]),
                ),
                None,
            )

        last_error = (
            f"clip-{index:02d} exceeded {VISION_MAX_CLIP_BYTES // (1024 * 1024)} MB after {profile['name']} compression "
            f"({size_bytes / (1024 * 1024):.2f} MB)"
        )
        remove_paths([output_path])

    return None, last_error or f"clip-{index:02d} could not be compressed"



def _invoke_dashscope(
    *,
    clip: PreparedClip,
    rules: list[HazardRule],
    selected_model: str,
    extra_context: str = "",
    retrieved_rules: bool = False,
) -> ProviderResponsePayload:
    video_bytes = clip.path.read_bytes()
    if len(video_bytes) > VISION_MAX_CLIP_BYTES:
        raise RuntimeError(
            f"Prepared clip is {len(video_bytes) / (1024 * 1024):.2f} MB, above the configured {VISION_MAX_CLIP_BYTES / (1024 * 1024):.2f} MB limit"
        )

    encoded_video = base64.b64encode(video_bytes).decode("ascii")
    body = {
        "model": selected_model,
        "temperature": VISION_TEMPERATURE,
        "max_tokens": 1200,
        "stream": False,
        "response_format": {"type": "json_object"},
        "enable_thinking": VISION_ENABLE_THINKING,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an industrial safety video reviewer. "
                    "Report hazards supported by the video and/or YOLO detections. "
                    "Only use the listed rule_id values. Return valid JSON only."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_prompt(clip=clip, rules=rules, extra_context=extra_context, retrieved_rules=retrieved_rules)},
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{encoded_video}"},
                    },
                ],
            },
        ],
    }

    endpoint = f"{VISION_API_URL.rstrip('/')}/chat/completions"
    last_error: Exception | None = None
    max_attempts = max(1, VISION_MAX_RETRIES + 1)

    for attempt in range(1, max_attempts + 1):
        try:
            req = request.Request(
                endpoint,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {VISION_API_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=VISION_REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            parsed = _parse_provider_payload(payload)
            message = {}
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                maybe_message = choices[0].get("message")
                if isinstance(maybe_message, dict):
                    message = maybe_message
            write_llm_log(
                source="batch_clip",
                clip_index=f"clip-{clip.index:02d}",
                model=selected_model,
                raw_response=message.get("content", payload),
                parsed_payload=parsed,
                notes=list(parsed.notes),
                prompt_section=extra_context,
                extra={
                    "clip_index": clip.index,
                    "clip_start_offset_sec": clip.start_offset_sec,
                    "clip_duration_sec": clip.duration_sec,
                    "clip_byte_size": clip.byte_size,
                    "retrieved_rules": retrieved_rules,
                    "findings": len(parsed.findings),
                    "attempt": attempt,
                },
            )
            return parsed
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}")
            if exc.code in {408, 409, 429, 500, 502, 503, 504} and attempt < max_attempts:
                continue
            break
        except (error.URLError, TimeoutError, ValidationError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < max_attempts:
                continue
            break

    try:
        write_llm_log(
            source="batch_clip",
            clip_index=f"clip-{clip.index:02d}",
            model=selected_model,
            raw_response="",
            parsed_payload=None,
            notes=[],
            diagnostics=[str(last_error) if last_error else "DashScope request failed"],
            prompt_section=extra_context,
            extra={
                "clip_index": clip.index,
                "clip_start_offset_sec": clip.start_offset_sec,
                "clip_duration_sec": clip.duration_sec,
                "clip_byte_size": clip.byte_size,
                "retrieved_rules": retrieved_rules,
                "error": str(last_error) if last_error else "DashScope request failed",
            },
        )
    except Exception:
        pass
    raise RuntimeError(str(last_error) if last_error else "DashScope request failed")



def _build_prompt(*, clip: PreparedClip, rules: list[HazardRule], extra_context: str = "", retrieved_rules: bool = False) -> str:
    rule_lines = "\n".join(_compact_rule_line(rule) for rule in rules)
    if retrieved_rules:
        rules_section = (
            "Retrieved Rules:\n"
            "The following rules were retrieved from the inspection rule database based on YOLO detections and clip context.\n"
            "Only use these rule_id values.\n"
            f"{rule_lines}\n"
        )
        extra_constraints = (
            "- Retrieved rules and YOLO detections are strong candidate hints.\n"
            "- Report a finding when YOLO and/or the video support the retrieved rule.\n"
        )
    else:
        rules_section = f"Rules:\n{rule_lines}\n"
        extra_constraints = ""

    prompt = (
        "Inspect this industrial inspection video clip and identify safety hazards that match the listed rules.\n"
        f"Clip start offset: {clip.start_offset_sec:.1f}s\n"
        f"Clip duration: {clip.duration_sec:.1f}s\n"
        f"Maximum findings to return: {VISION_MAX_FINDINGS_PER_CLIP}\n"
        "Return exactly one JSON object with this shape:\n"
        '{"findings":[{"rule_id":"rule-001","start_offset_sec":1.2,"end_offset_sec":4.8,"evidence_sec":[2.0,4.0],"description":"what is visible","confidence":0.84}],"notes":["optional short notes"]}\n'
        f"{rules_section}"
        "Constraints:\n"
        "- Use only the listed rule_id values.\n"
        "- Offsets must be relative to this clip, not the full video.\n"
        "- Prefer reporting a finding when YOLO detections and/or the video support a listed rule "
        "(typical object→rule mappings: sander/woodmachine/planer/rotating parts → rule-021; "
        "dust/dustcover/dustduct → rule-104; exit/passage blockage → rule-056; "
        "missing no-smoking signs near combustibles → rule-053).\n"
        "- Do not invent hazards with zero support from video or YOLO.\n"
        f"{extra_constraints}"
        "- Return {\"findings\":[],\"notes\":[...]} only when neither the video nor YOLO detections "
        "indicate any listed hazard in this clip."
    )
    context = extra_context.strip()
    if context:
        prompt += (
            "\n\n"
            "=== YOLO pre-analysis context (append to the same user message as the video) ===\n"
            f"{context}\n"
            "=== End YOLO context ===\n"
            "Use YOLO detections together with the attached video. "
            "When YOLO already flags relevant objects, actively check the correlated rules and "
            "report findings if the clip is consistent with those hazards — do not discard them "
            "only because evidence is partially occluded or slightly blurry."
        )
    return prompt



def _compact_rule_line(rule: HazardRule) -> str:
    evidence_objects = []
    try:
        evidence_objects = json.loads(rule.evidence_objects_json)
    except json.JSONDecodeError:
        evidence_objects = []
    hint_parts = [
        _clip_text(rule.hazard_desc, 72),
    ]
    if rule.checker_scope:
        hint_parts.append(f"scope={_clip_text(rule.checker_scope, 24)}")
    if evidence_objects:
        hint_parts.append(f"focus={_clip_text(', '.join(evidence_objects[:3]), 30)}")
    return f"- {rule.rule_id} | severity={rule.severity} | {' | '.join(hint_parts)}"



def _parse_provider_payload(payload: dict[str, object]) -> ProviderResponsePayload:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("Provider response does not contain choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError("Provider choice payload is invalid")
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Provider response does not contain a message")

    content = message.get("content")
    json_payload = _normalize_message_content(content)
    try:
        return ProviderResponsePayload.model_validate(json_payload)
    except ValidationError as exc:
        raise RuntimeError(f"Provider JSON did not match schema: {exc}") from exc



def _normalize_message_content(content: object) -> dict[str, object]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text_fragments: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_fragments.append(item)
            elif isinstance(item, dict):
                text_value = item.get("text") or item.get("content")
                if isinstance(text_value, str):
                    text_fragments.append(text_value)
        content = "\n".join(fragment for fragment in text_fragments if fragment)
    if not isinstance(content, str):
        raise RuntimeError("Provider content is empty")
    stripped = content.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
        if match:
            stripped = match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end >= start:
        stripped = stripped[start : end + 1]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Provider returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Provider JSON root must be an object")
    return parsed



def _clip_findings_to_seeds(
    clip: PreparedClip,
    payload: ProviderResponsePayload,
    rules_by_id: dict[str, HazardRule],
) -> list[FindingSeed]:
    seeds: list[FindingSeed] = []
    for item in payload.findings[:VISION_MAX_FINDINGS_PER_CLIP]:
        rule = rules_by_id.get(item.rule_id)
        if rule is None:
            continue

        start_offset_sec = _clamp(item.start_offset_sec, 0.0, clip.duration_sec)
        end_offset_sec = _clamp(item.end_offset_sec, start_offset_sec, clip.duration_sec)
        if end_offset_sec <= start_offset_sec:
            end_offset_sec = min(clip.duration_sec, start_offset_sec + 1.5)

        evidence_offsets = item.evidence_sec or [start_offset_sec, (start_offset_sec + end_offset_sec) / 2, end_offset_sec]
        evidence_frame_ts = sorted(
            {
                clip.start_ts_ms + int(round(_clamp(offset, 0.0, clip.duration_sec) * 1000))
                for offset in evidence_offsets
            }
        )
        description = item.description.strip() or rule.hazard_desc
        seeds.append(
            FindingSeed(
                rule_id=rule.rule_id,
                title=_short_title(rule.hazard_desc),
                time_start_ms=clip.start_ts_ms + int(round(start_offset_sec * 1000)),
                time_end_ms=clip.start_ts_ms + int(round(end_offset_sec * 1000)),
                evidence_frame_ts=evidence_frame_ts,
                description=description,
                confidence=max(0.05, min(0.99, round(item.confidence, 2))),
                severity=rule.severity,
            )
        )
    return seeds



def _dedupe_seeds(
    seeds: list[FindingSeed],
    *,
    same_time_window_ms: int | None = None,
) -> list[FindingSeed]:
    """Collapse duplicate findings for the same rule.

    Default: round start/end to whole seconds (legacy provider behavior).
    When ``same_time_window_ms`` is set, also merge same-rule findings whose
    start times fall within that window (mosaic views of one moment).
    """
    if same_time_window_ms is None or int(same_time_window_ms) <= 0:
        deduped: dict[tuple[str, int, int], FindingSeed] = {}
        for seed in seeds:
            key = (seed.rule_id, round(seed.time_start_ms / 1000), round(seed.time_end_ms / 1000))
            current = deduped.get(key)
            if current is None or seed.confidence > current.confidence:
                deduped[key] = seed
        return sorted(deduped.values(), key=lambda item: (item.time_start_ms, item.rule_id))

    window = int(same_time_window_ms)
    # Keep a stable cluster origin even when a later, higher-confidence seed is
    # selected as the representative. Otherwise the window can drift forever.
    kept: list[tuple[int, FindingSeed]] = []
    for seed in sorted(seeds, key=lambda item: (item.rule_id, item.time_start_ms, -item.confidence)):
        match_index = None
        for index, (cluster_start_ms, current) in enumerate(kept):
            if current.rule_id != seed.rule_id:
                continue
            if abs(cluster_start_ms - int(seed.time_start_ms)) > window:
                continue
            match_index = index
            break
        if match_index is None:
            kept.append((int(seed.time_start_ms), seed))
            continue
        cluster_start_ms, current = kept[match_index]
        if seed.confidence > current.confidence:
            kept[match_index] = (cluster_start_ms, seed)
    return sorted(
        (item for _cluster_start_ms, item in kept),
        key=lambda item: (item.time_start_ms, item.rule_id),
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
) -> AnalysisRunResult:
    unique_notes = list(dict.fromkeys(note for note in notes if note))
    unique_diagnostics = list(dict.fromkeys(diag for diag in diagnostics if diag))
    summary = {
        "analysis_mode": "provider",
        "analysis_provider": provider_label(),
        "analysis_model": selected_model,
        "provider_available": provider_available(selected_model),
        "status": status,
        "clip_seconds": VISION_CLIP_SECONDS,
        "video_fps": VISION_VIDEO_FPS,
        "max_findings_per_clip": VISION_MAX_FINDINGS_PER_CLIP,
        "clip_count": clip_count,
        "successful_clips": successful_clips,
        "failed_clips": max(0, clip_count - successful_clips),
        "notes": unique_notes,
        "diagnostics": unique_diagnostics,
    }
    return AnalysisRunResult(status=status, findings=findings, summary=summary)



def _short_title(text: str) -> str:
    compact = text.replace("\u2605", "").replace("*", "").strip()
    return compact[:44] + ("..." if len(compact) > 44 else "")



def _clip_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."



def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
