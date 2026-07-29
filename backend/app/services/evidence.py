from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..models import Finding, Project
from ..settings import CLIP_COMPRESS_TIMEOUT_SECONDS, FFMPEG_BIN
from .storage import read_json, remove_paths, resolve_project_path

logger = logging.getLogger(__name__)
_EVIDENCE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evidence")


def cache_evidence_frames(
    project: Project,
    findings: list[Finding],
    *,
    replace_all: bool = True,
) -> dict[str, object]:
    """Extract evidence frames from the project inspection video.

    When ``replace_all`` is False (monitor path), existing frames are kept and
    only missing timestamps are extracted.
    """
    video_path = resolve_project_path(
        project.artifacts_dir or "",
        project.inspection_video_path,
        "artifacts/inspection.mp4",
    )
    if not video_path.exists():
        raise FileNotFoundError("Project video is missing")

    evidence_dir = _evidence_dir(project)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    removed_bytes = 0
    if replace_all:
        removed_bytes = remove_paths(list(evidence_dir.glob("frame_*.jpg")))

    timestamps = _finding_timestamps(findings)
    if not timestamps:
        return {"frame_count": 0, "removed_bytes": removed_bytes}

    video_start_ts = _video_start_ts(project)

    if replace_all:
        pending = timestamps
    else:
        pending = [ts for ts in timestamps if not (evidence_dir / f"frame_{ts}.jpg").exists()]

    written = _extract_frames_parallel(
        video_path=video_path,
        timestamps=pending,
        video_start_ts=video_start_ts,
        evidence_dir=evidence_dir,
    )
    return {"frame_count": written, "removed_bytes": removed_bytes}


def append_evidence_frames_from_clip(
    project: Project,
    findings: list[Finding],
    *,
    clip_path: Path,
    clip_start_ts_ms: int,
) -> dict[str, object]:
    """Append evidence frames extracted from a short segment clip (no wipe)."""
    if not clip_path.exists():
        raise FileNotFoundError(f"Evidence clip not found: {clip_path}")

    evidence_dir = _evidence_dir(project)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    timestamps = _finding_timestamps(findings)
    missing = [ts for ts in timestamps if not (evidence_dir / f"frame_{ts}.jpg").exists()]
    if not missing:
        return {"frame_count": 0, "removed_bytes": 0}

    written = _extract_frames_parallel(
        video_path=clip_path,
        timestamps=missing,
        video_start_ts=clip_start_ts_ms,
        evidence_dir=evidence_dir,
    )
    return {"frame_count": written, "removed_bytes": 0}


def cached_frame_path(project: Project, timestamp_ms: int) -> Path | None:
    path = _evidence_dir(project) / f"frame_{timestamp_ms}.jpg"
    if path.exists():
        return path
    return None


def _finding_timestamps(findings: list[Finding]) -> list[int]:
    return sorted(
        {
            timestamp
            for finding in findings
            for timestamp in [finding.time_start_ms, *json.loads(finding.evidence_frame_ts_json), finding.time_end_ms]
        }
    )


def _evidence_dir(project: Project) -> Path:
    return Path(project.artifacts_dir) / "evidence_frames"


def _video_start_ts(project: Project) -> int:
    dataset_summary_path = Path(project.artifacts_dir) / "summaries" / "dataset_summary.json"
    if not dataset_summary_path.exists():
        raise FileNotFoundError("Dataset summary is missing")
    dataset_summary = read_json(dataset_summary_path)
    value = dataset_summary.get("video_start_ts")
    if value is None:
        raise ValueError("Dataset summary is missing video_start_ts")
    return int(value)


def _extract_frames_parallel(
    *,
    video_path: Path,
    timestamps: list[int],
    video_start_ts: int,
    evidence_dir: Path,
) -> int:
    """Extract frames in parallel with per-timestamp ffmpeg invocations.

    Each timestamp gets a dedicated ffmpeg call with exact seek (preserving the
    original correctness guarantees).  A bounded thread pool ensures we never
    start more than 4 concurrent ffmpeg processes.
    """
    if not timestamps:
        return 0

    futures = {}
    for timestamp_ms in timestamps:
        output_path = evidence_dir / f"frame_{timestamp_ms}.jpg"
        futures[
            _EVIDENCE_EXECUTOR.submit(
                _extract_one_frame,
                video_path=video_path,
                timestamp_ms=timestamp_ms,
                video_start_ts=video_start_ts,
                output_path=output_path,
            )
        ] = output_path

    written = 0
    for future in as_completed(futures):
        success = future.result()
        if success:
            written += 1
        else:
            output_path = futures[future]
            output_path.unlink(missing_ok=True)

    return written


def _extract_one_frame(
    *,
    video_path: Path,
    timestamp_ms: int,
    video_start_ts: int,
    output_path: Path,
) -> bool:
    """Run ffmpeg to extract one frame at the given timestamp.  Returns True on success."""
    relative_seconds = max(0.0, (timestamp_ms - video_start_ts) / 1000.0)
    command = [
        FFMPEG_BIN,
        "-y",
        "-ss",
        f"{relative_seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=CLIP_COMPRESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("Evidence frame extraction timed out at %sms", timestamp_ms)
        return False
    if result.returncode != 0:
        logger.warning("Evidence frame extraction failed at %sms: %s", timestamp_ms, result.stderr.strip() or "unknown")
        return False
    return True
