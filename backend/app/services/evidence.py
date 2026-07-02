from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..models import Finding, Project
from ..settings import FFMPEG_BIN
from .storage import read_json, remove_paths


def cache_evidence_frames(project: Project, findings: list[Finding]) -> dict[str, object]:
    if not project.inspection_video_path:
        raise FileNotFoundError("Project video is missing")

    evidence_dir = _evidence_dir(project)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    removed_bytes = remove_paths(list(evidence_dir.glob("frame_*.jpg")))

    timestamps = sorted(
        {
            timestamp
            for finding in findings
            for timestamp in [finding.time_start_ms, *json.loads(finding.evidence_frame_ts_json), finding.time_end_ms]
        }
    )
    if not timestamps:
        return {"frame_count": 0, "removed_bytes": removed_bytes}

    video_start_ts = _video_start_ts(project)
    for timestamp_ms in timestamps:
        output_path = evidence_dir / f"frame_{timestamp_ms}.jpg"
        _extract_frame(
            video_path=Path(project.inspection_video_path),
            timestamp_ms=timestamp_ms,
            video_start_ts=video_start_ts,
            output_path=output_path,
        )
    return {"frame_count": len(timestamps), "removed_bytes": removed_bytes}


def cached_frame_path(project: Project, timestamp_ms: int) -> Path | None:
    path = _evidence_dir(project) / f"frame_{timestamp_ms}.jpg"
    if path.exists():
        return path
    return None


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


def _extract_frame(*, video_path: Path, timestamp_ms: int, video_start_ts: int, output_path: Path) -> None:
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
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg evidence extraction failed")
