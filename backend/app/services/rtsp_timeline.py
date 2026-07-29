"""Resolve recording video clocks from RTSP-transmitted timeline data.

The local test publisher burns a machine-readable timestamp barcode into the
``/time`` (and optionally ``/live``) stream while PTS is remapped onto the map
epoch. Recorders must prefer that timeline over wall-clock / filename time so
map trajectory timestamps and video clocks share one origin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from ..settings import FFMPEG_BIN


logger = logging.getLogger(__name__)

# Must match generate_rtsp_stream._timestamp_barcode_* constants.
TIMESTAMP_BARCODE_BITS = 48
TIMESTAMP_BARCODE_BIT_WIDTH = 8
TIMESTAMP_BARCODE_HEIGHT = 16

RTSP_TIMELINE_PROBE_TIMEOUT_SEC = float(os.getenv("RTSP_TIMELINE_PROBE_TIMEOUT_SEC", "12"))
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").strip() or "tcp"


@dataclass(frozen=True)
class RtspTimelineSample:
    timestamp_ms: int
    source: str


def recording_timeline_meta_path(recording_path: Path) -> Path:
    return recording_path.with_name(f"{recording_path.stem}.meta.json")


def write_recording_timeline_meta(
    recording_path: Path,
    *,
    video_start_ts: int,
    source: str,
    rtsp_url: str = "",
) -> Path:
    path = recording_timeline_meta_path(recording_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "video_start_ts": int(video_start_ts),
        "source": source,
        "rtsp_url": rtsp_url,
        "sampled_at_wall_ms": _utc_now_ms(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_recording_timeline_meta(recording_path: Path) -> RtspTimelineSample | None:
    path = recording_timeline_meta_path(recording_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        timestamp_ms = int(payload.get("video_start_ts") or 0)
    except (TypeError, ValueError):
        return None
    if timestamp_ms <= 0:
        return None
    source = str(payload.get("source") or "meta").strip() or "meta"
    return RtspTimelineSample(timestamp_ms=timestamp_ms, source=source)


def derive_time_rtsp_url(video_rtsp_url: str) -> str | None:
    """Derive the companion ``/time`` URL from a video RTSP URL when possible."""
    raw = video_rtsp_url.strip()
    if not raw.lower().startswith("rtsp://"):
        return None
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    if path.endswith("/live"):
        new_path = f"{path[: -len('/live')]}/time"
    elif path.rsplit("/", 1)[-1] == "live":
        parent = path.rsplit("/", 1)[0]
        new_path = f"{parent}/time" if parent else "/time"
    else:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        new_path = f"{parent}/time" if parent else "/time"
    if not new_path.startswith("/"):
        new_path = f"/{new_path}"
    return urlunparse(parsed._replace(path=new_path))


def decode_timestamp_ms_from_image(image_path: Path) -> int | None:
    """Decode the top-left binary timestamp barcode burned into a time/video frame."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            needed_w = TIMESTAMP_BARCODE_BITS * TIMESTAMP_BARCODE_BIT_WIDTH
            if width < needed_w or height < TIMESTAMP_BARCODE_HEIGHT:
                return None
            value = 0
            for bit in range(TIMESTAMP_BARCODE_BITS):
                x0 = bit * TIMESTAMP_BARCODE_BIT_WIDTH
                total = 0
                count = 0
                for y in range(TIMESTAMP_BARCODE_HEIGHT):
                    for x in range(x0, x0 + TIMESTAMP_BARCODE_BIT_WIDTH):
                        r, g, b = rgb.getpixel((x, y))
                        total += int(r) + int(g) + int(b)
                        count += 1
                if count <= 0:
                    return None
                if (total / count) >= 128 * 3 * 0.45:
                    value |= 1 << bit
            if value <= 0:
                return None
            # Sanity: reject obviously broken barcodes (pre-2001 or far-future).
            if value < 1_000_000_000_000 or value > 4_000_000_000_000:
                return None
            return int(value)
    except OSError:
        return None


def capture_rtsp_frame_png(
    rtsp_url: str,
    output_path: Path,
    *,
    timeout_sec: float = RTSP_TIMELINE_PROBE_TIMEOUT_SEC,
) -> bool:
    """Grab a single decoded frame from an RTSP URL into a PNG file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        RTSP_TRANSPORT,
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-y",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_sec)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0


def sample_timestamp_ms_from_rtsp(rtsp_url: str) -> int | None:
    with tempfile.TemporaryDirectory(prefix="rtsp-timeline-") as tmp:
        frame_path = Path(tmp) / "frame.png"
        if not capture_rtsp_frame_png(rtsp_url, frame_path):
            return None
        return decode_timestamp_ms_from_image(frame_path)


def map_timeline_origin_ms(rtsp_url: str) -> int | None:
    """Fall back to the vehicle map's first trajectory timestamp (RTSP time-channel anchor)."""
    try:
        from .rtsp_recorder import resolve_storage_key_for_rtsp_url
        from .rtsp_vehicles import find_robot_map_scene
        from .scene_transport import load_compact_scene_json
    except Exception:
        return None

    try:
        storage_key = resolve_storage_key_for_rtsp_url(rtsp_url)
        map_path = find_robot_map_scene(storage_key)
        if map_path is None:
            return None
        payload = load_compact_scene_json(map_path)
    except Exception:
        logger.debug("Failed loading map timeline origin for %s", rtsp_url, exc_info=True)
        return None

    timestamps = payload.get("trajectory_timestamps") or []
    if not isinstance(timestamps, list) or not timestamps:
        # Cheap path for huge legacy scenes: peek the first integer in the array.
        peeked = _peek_first_trajectory_timestamp(map_path)
        return peeked
    try:
        return int(timestamps[0])
    except (TypeError, ValueError):
        return None


def _peek_first_trajectory_timestamp(map_path: Path) -> int | None:
    try:
        with map_path.open("r", encoding="utf-8") as handle:
            chunk = handle.read(4_000_000)
    except OSError:
        return None
    match = re.search(r'"trajectory_timestamps"\s*:\s*\[\s*(-?\d+)', chunk)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_recording_video_start_ts(rtsp_url: str) -> RtspTimelineSample:
    """Pick the video clock origin for a new recording (RTSP timeline preferred)."""
    candidates: list[str] = []
    time_url = derive_time_rtsp_url(rtsp_url)
    if time_url:
        candidates.append(time_url)
    candidates.append(rtsp_url.strip())

    seen: set[str] = set()
    for url in candidates:
        normalized = url.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        sampled = sample_timestamp_ms_from_rtsp(normalized)
        if sampled is not None:
            source = "rtsp_time_barcode" if normalized == time_url else "rtsp_video_barcode"
            logger.info("RTSP timeline sample %s from %s", sampled, source)
            return RtspTimelineSample(timestamp_ms=sampled, source=source)

    map_origin = map_timeline_origin_ms(rtsp_url)
    if map_origin is not None:
        logger.warning(
            "RTSP timestamp barcode unavailable for %s; using map timeline origin %s",
            rtsp_url,
            map_origin,
        )
        return RtspTimelineSample(timestamp_ms=map_origin, source="map_timeline_origin")

    wall = _utc_now_ms()
    logger.warning(
        "RTSP timeline and map origin unavailable for %s; falling back to wall clock %s",
        rtsp_url,
        wall,
    )
    return RtspTimelineSample(timestamp_ms=wall, source="wall_clock")


def scene_timeline_overlaps_video(
    scene: dict[str, object],
    video_start_ts: int,
    video_end_ts: int,
) -> bool:
    timestamps_raw = scene.get("trajectory_timestamps") or []
    if not isinstance(timestamps_raw, list) or not timestamps_raw:
        return False
    try:
        map_start = int(timestamps_raw[0])
        map_end = int(timestamps_raw[-1])
    except (TypeError, ValueError):
        return False
    start_ts = int(video_start_ts)
    end_ts = int(video_end_ts)
    if end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts
    return map_start <= end_ts and map_end >= start_ts


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
