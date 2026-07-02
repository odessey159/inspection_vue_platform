from __future__ import annotations

import subprocess
from pathlib import Path

from ..settings import FFMPEG_BIN
from .storage import write_json


def build_video(video_files: list[Path], timestamps: list[int], output_path: Path, manifest_path: Path, fps: float) -> dict[str, object]:
    if not video_files:
        raise ValueError("No video frames found")

    concat_path = manifest_path.parent / "frames.ffconcat"
    median_gap_ms = _median([b - a for a, b in zip(timestamps, timestamps[1:])]) if len(timestamps) > 1 else 1000
    lines = ["ffconcat version 1.0"]
    for index, frame_path in enumerate(video_files):
        safe_path = frame_path.as_posix().replace("'", "\\'")
        lines.append(f"file '{safe_path}'")
        gap_ms = median_gap_ms if index == len(video_files) - 1 else max(1, timestamps[index + 1] - timestamps[index])
        lines.append(f"duration {gap_ms / 1000:.6f}")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    keyframe_interval = max(1, round(float(fps)))

    try:
        command = [
            FFMPEG_BIN,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-vf",
            f"fps={fps:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-g",
            str(keyframe_interval),
            "-keyint_min",
            str(keyframe_interval),
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        _run_ffmpeg(command)
    finally:
        concat_path.unlink(missing_ok=True)

    payload = {
        "videoPath": str(output_path),
        "fps": fps,
        "frameCount": len(video_files),
        "startTs": timestamps[0],
        "endTs": timestamps[-1],
        "clips": [],
    }
    write_json(manifest_path, payload)
    return payload


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg failed")


def _median(values: list[int]) -> int:
    if not values:
        return 1000
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return int(ordered[middle])
    return int((ordered[middle - 1] + ordered[middle]) / 2)
