"""Resolve recording video clocks from RTSP-transmitted timeline data.

Default path: read every encoded picture's H.264/H.265 ``user_data_unregistered``
pose SEI (``timestamp_ns, x, y, yaw``) from the recorded bitstream and store a
1:1 ``.frames.jsonl`` sidecar next to the video. Live recordings still probe
the stream for a provisional ``video_start_ts`` before ffmpeg starts.

Fallback: the local test publisher burns a machine-readable timestamp barcode
into the ``/time`` (and optionally ``/live``) stream. Recorders still prefer an
RTSP-transmitted timeline over wall-clock / filename time so map trajectory
timestamps and video clocks share one origin.

Set ``RTSP_TIMELINE_PARSER=barcode`` to skip SEI and use the barcode path only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from ..settings import FFMPEG_BIN, RTSP_RECORDINGS_DIR, RTSP_TIMELINE_PARSER
from .rtsp_sei import (
    FrameMetadata,
    PictureMetadata,
    extract_picture_metadata_from_video,
    first_pose_from_pictures,
    sample_sei_from_rtsp,
)


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
    x: float | None = None
    y: float | None = None
    yaw: float | None = None


def recording_timeline_meta_path(recording_path: Path) -> Path:
    return recording_path.with_name(f"{recording_path.stem}.meta.json")


def recording_frames_path(recording_path: Path) -> Path:
    """Sidecar with one JSONL row per encoded picture, aligned to the video file."""
    return recording_path.with_name(f"{recording_path.stem}.frames.jsonl")


def recording_sidecar_paths(recording_path: Path) -> tuple[Path, Path]:
    return recording_timeline_meta_path(recording_path), recording_frames_path(recording_path)


def write_recording_timeline_meta(
    recording_path: Path,
    *,
    video_start_ts: int,
    source: str,
    rtsp_url: str = "",
    x: float | None = None,
    y: float | None = None,
    yaw: float | None = None,
    frame_count: int | None = None,
    sei_frame_count: int | None = None,
) -> Path:
    path = recording_timeline_meta_path(recording_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            payload.update(existing)
    payload["video_start_ts"] = int(video_start_ts)
    payload["source"] = source
    payload["sampled_at_wall_ms"] = _utc_now_ms()
    if rtsp_url:
        payload["rtsp_url"] = rtsp_url
    elif "rtsp_url" not in payload:
        payload["rtsp_url"] = ""
    if x is not None:
        payload["sei_x"] = float(x)
    if y is not None:
        payload["sei_y"] = float(y)
    if yaw is not None:
        payload["sei_yaw"] = float(yaw)
    if frame_count is not None:
        payload["frame_count"] = int(frame_count)
        payload["frames_path"] = recording_frames_path(recording_path).name
    if sei_frame_count is not None:
        payload["sei_frame_count"] = int(sei_frame_count)
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
    return RtspTimelineSample(
        timestamp_ms=timestamp_ms,
        source=source,
        x=_optional_float(payload.get("sei_x")),
        y=_optional_float(payload.get("sei_y")),
        yaw=_optional_float(payload.get("sei_yaw")),
    )


def write_recording_frame_records(recording_path: Path, pictures: tuple[PictureMetadata, ...] | list[PictureMetadata]) -> Path:
    path = recording_frames_path(recording_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(picture.to_record(), ensure_ascii=False) for picture in pictures]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def load_recording_frame_records(recording_path: Path) -> tuple[PictureMetadata, ...]:
    path = recording_frames_path(recording_path)
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    pictures: list[PictureMetadata] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        picture = PictureMetadata.from_record(payload)
        if picture is not None:
            pictures.append(picture)
    return tuple(pictures)


def lookup_frame_metadata_for_timestamp(
    pictures: tuple[PictureMetadata, ...] | list[PictureMetadata],
    timestamp_ms: int,
) -> PictureMetadata | None:
    """Return the picture whose SEI timestamp is nearest to ``timestamp_ms``."""
    dated = [picture for picture in pictures if picture.metadata is not None]
    if not dated:
        return None
    target = int(timestamp_ms)
    return min(dated, key=lambda picture: abs((picture.metadata.timestamp_ms if picture.metadata else 0) - target))


def persist_recording_frame_metadata(
    recording_path: Path,
    *,
    vehicle_id: str | None = None,
) -> tuple[PictureMetadata, ...]:
    """Read every encoded picture's pose SEI from a finished recording and store it 1:1.

    Full-frame JPEGs are not dumped here: a 25 fps recording would explode disk.
    The video file remains the image store; ``.frames.jsonl`` is the matching
    metadata row for each picture. Evidence JPEGs get their own ``.json`` sidecar.
    """
    if not recording_path.is_file() or recording_path.stat().st_size <= 16:
        return ()
    try:
        pictures = extract_picture_metadata_from_video(recording_path)
    except (OSError, ValueError) as error:
        logger.warning("Failed to extract per-frame pose SEI from %s: %s", recording_path, error)
        return load_recording_frame_records(recording_path)

    if not pictures:
        logger.info("No per-picture pose SEI found in %s", recording_path)
        return ()

    write_recording_frame_records(recording_path, pictures)
    pose = first_pose_from_pictures(pictures)
    existing = read_recording_timeline_meta(recording_path)
    if pose is None and existing is None:
        logger.info("Stored %s picture rows without pose SEI for %s", len(pictures), recording_path.name)
        return pictures
    video_start_ts = pose.timestamp_ms if pose is not None else existing.timestamp_ms
    source = "rtsp_sei" if pose is not None else existing.source
    write_recording_timeline_meta(
        recording_path,
        video_start_ts=video_start_ts,
        source=source,
        x=None if pose is None else pose.x,
        y=None if pose is None else pose.y,
        yaw=None if pose is None else pose.yaw,
        frame_count=len(pictures),
        sei_frame_count=sum(1 for picture in pictures if picture.metadata is not None),
    )
    logger.info(
        "Stored %s picture SEI rows (%s with pose) for %s",
        len(pictures),
        sum(1 for picture in pictures if picture.metadata is not None),
        recording_path.name,
    )
    _extend_vehicle_trajectory_from_pictures(recording_path, pictures, vehicle_id=vehicle_id)
    return pictures


def infer_storage_key_from_recording_path(recording_path: Path) -> str | None:
    try:
        relative = recording_path.resolve().relative_to(RTSP_RECORDINGS_DIR.resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    key = str(relative.parts[0]).strip()
    return key or None


def _extend_vehicle_trajectory_from_pictures(
    recording_path: Path,
    pictures: tuple[PictureMetadata, ...] | list[PictureMetadata],
    *,
    vehicle_id: str | None = None,
) -> None:
    cleaned = (vehicle_id or "").strip() or infer_storage_key_from_recording_path(recording_path)
    if not cleaned:
        return
    try:
        from .vehicle_trajectory import extend_vehicle_trajectory_from_pictures

        extend_vehicle_trajectory_from_pictures(cleaned, pictures)
    except Exception:
        logger.debug("Failed extending vehicle trajectory from %s", recording_path, exc_info=True)


def ensure_recording_frame_metadata(recording_path: Path) -> tuple[PictureMetadata, ...]:
    pictures = load_recording_frame_records(recording_path)
    if pictures:
        _extend_vehicle_trajectory_from_pictures(recording_path, pictures)
        return pictures
    return persist_recording_frame_metadata(recording_path)


def copy_recording_timeline_sidecars(source_video: Path, destination_video: Path) -> None:
    for sidecar_for in (recording_timeline_meta_path, recording_frames_path):
        source = sidecar_for(source_video)
        if not source.is_file():
            continue
        destination = sidecar_for(destination_video)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def delete_recording_timeline_sidecars(recording_path: Path) -> int:
    deleted = 0
    for sidecar in recording_sidecar_paths(recording_path):
        if not sidecar.is_file():
            continue
        try:
            sidecar.unlink()
            deleted += 1
        except OSError:
            continue
    return deleted


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
    """Decode the burned-in timestamp barcode from one RTSP frame (legacy path)."""
    with tempfile.TemporaryDirectory(prefix="rtsp-timeline-") as tmp:
        frame_path = Path(tmp) / "frame.png"
        if not capture_rtsp_frame_png(rtsp_url, frame_path):
            return None
        return decode_timestamp_ms_from_image(frame_path)


def map_timeline_origin_ms(rtsp_url: str) -> int | None:
    """Fall back to the vehicle's RTSP-grown trajectory origin (not the map file)."""
    try:
        from .rtsp_recorder import resolve_storage_key_for_rtsp_url
        from .vehicle_trajectory import load_vehicle_trajectory
    except Exception:
        return None

    try:
        storage_key = resolve_storage_key_for_rtsp_url(rtsp_url)
        record = load_vehicle_trajectory(storage_key)
    except Exception:
        logger.debug("Failed loading vehicle trajectory origin for %s", rtsp_url, exc_info=True)
        return None

    if not record.trajectory_timestamps:
        return None
    try:
        value = int(record.trajectory_timestamps[0])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_recording_video_start_ts(rtsp_url: str) -> RtspTimelineSample:
    """Pick the video clock origin for a new recording (SEI first, barcode fallback)."""
    parser = _timeline_parser()
    if parser == "sei":
        sei_sample = _sample_sei_timeline(rtsp_url)
        if sei_sample is not None:
            logger.info(
                "RTSP timeline sample %s from %s",
                sei_sample.timestamp_ms,
                sei_sample.source,
            )
            return sei_sample
        logger.info("RTSP pose SEI unavailable for %s; falling back to timestamp barcode", rtsp_url)

    barcode_sample = _sample_barcode_timeline(rtsp_url)
    if barcode_sample is not None:
        logger.info(
            "RTSP timeline sample %s from %s",
            barcode_sample.timestamp_ms,
            barcode_sample.source,
        )
        return barcode_sample

    map_origin = map_timeline_origin_ms(rtsp_url)
    if map_origin is not None:
        logger.warning(
            "RTSP SEI and timestamp barcode unavailable for %s; using map timeline origin %s",
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


def _timeline_parser() -> str:
    value = (RTSP_TIMELINE_PARSER or "sei").strip().lower()
    return value if value in {"sei", "barcode"} else "sei"


def _sample_sei_timeline(rtsp_url: str) -> RtspTimelineSample | None:
    metadata = sample_sei_from_rtsp(rtsp_url.strip())
    if metadata is None:
        return None
    return _timeline_sample_from_sei(metadata)


def _timeline_sample_from_sei(metadata: FrameMetadata) -> RtspTimelineSample:
    return RtspTimelineSample(
        timestamp_ms=metadata.timestamp_ms,
        source="rtsp_sei",
        x=metadata.x,
        y=metadata.y,
        yaw=metadata.yaw,
    )


def _sample_barcode_timeline(rtsp_url: str) -> RtspTimelineSample | None:
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
        if sampled is None:
            continue
        source = "rtsp_time_barcode" if normalized == time_url else "rtsp_video_barcode"
        return RtspTimelineSample(timestamp_ms=sampled, source=source)
    return None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
