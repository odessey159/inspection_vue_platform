"""RTSP recording, playback, project import, and localhost fallback for Docker.

Recording clocks prefer ``rtsp_timeline`` (pose SEI, then stream barcode / map
origin) over wall clock. Scene import aligns onboard maps onto that video
timeline and upgrades placeholder scenes when ``robots/<id>/maps/scene.json``
appears.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlmodel import Session

from ..models import Project
from ..settings import (
    DEFAULT_STANDARDS_DIR,
    FFMPEG_BIN,
    RTSP_RECORDING_CLEANUP_ENABLED,
    RTSP_RECORDING_CLEANUP_INTERVAL_SECONDS,
    RTSP_RECORDINGS_DIR,
    YOLO_API_URL,
)
from .rules import export_rules_payload, parse_rules, sync_rules_to_db
from .storage import ensure_project_dirs_for, path_size_bytes, read_json, resolve_project_path, to_project_relative_path, write_json


DEFAULT_RTSP_URL = os.getenv("RTSP_DEFAULT_URL", "rtsp://127.0.0.1:18554/live").strip()
DEFAULT_RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").strip() or "tcp"
DEFAULT_RTSP_RECORD_SECONDS = float(os.getenv("RTSP_RECORD_SECONDS", "60"))
RTSP_LOCALHOST_FALLBACK_HOST = os.getenv("RTSP_LOCALHOST_FALLBACK_HOST", "host.docker.internal").strip()
LOCALHOST_NAMES = {"127.0.0.1", "localhost", "::1"}
# Prefer stream copy for live RTSP recording (much lighter than libx264 re-encode).
RTSP_RECORD_VIDEO_CODEC = os.getenv("RTSP_RECORD_VIDEO_CODEC", "copy").strip().lower() or "copy"
# Socket I/O timeout for RTSP reads (microseconds). Passed as ffmpeg RTSP ``-timeout``.
# Debian/ffmpeg 7.x does not accept global ``-rw_timeout``; use the RTSP demuxer option.
RTSP_FFMPEG_RW_TIMEOUT_US = int(os.getenv("RTSP_FFMPEG_RW_TIMEOUT_US", "15000000"))
# Fragmented MP4 so watchdog recordings stay readable while still growing.
RTSP_LIVE_MOVFLAGS = "frag_keyframe+empty_moov+default_base_moof"
RTSP_FINISHED_MOVFLAGS = "+faststart"
# Short TTL so stacked UI/watchdog polls share one ffmpeg probe without changing
# observable online/offline behavior beyond existing poll intervals.
RTSP_PUBLISH_CACHE_TTL_SECONDS = float(os.getenv("RTSP_PUBLISH_CACHE_TTL_SECONDS", "2"))
# Allow slow-starting RTSP publishers enough time to deliver their first decodable frame.
RTSP_PUBLISH_PROBE_TIMEOUT_SECONDS = float(os.getenv("RTSP_PUBLISH_PROBE_TIMEOUT_SECONDS", "60"))

_cleanup_stop = threading.Event()
_cleanup_thread: threading.Thread | None = None
_publish_probe_lock = threading.Lock()
_publish_probe_cache: dict[str, tuple[float, bool]] = {}
_publish_probe_inflight: dict[str, threading.Event] = {}


@dataclass(frozen=True)
class RtspProjectSettings:
    rtsp_url: str
    duration_sec: float
    rtsp_transport: str
    storage_path: Path | None = None


@dataclass(frozen=True)
class RtspRecordingResult:
    rtsp_url: str
    output_path: Path
    playback_path: Path
    video_start_ts: int
    video_end_ts: int
    duration_sec: float
    fps: float
    frame_count: int


def is_rtsp_project(project: Project) -> bool:
    return (project.bag_dir or "").strip().lower().startswith("rtsp://")


def check_rtsp_server_reachable(rtsp_url: str, *, timeout: float = 1.5) -> str:
    return resolve_recording_rtsp_url(rtsp_url, timeout=timeout)


def is_rtsp_stream_reachable(rtsp_url: str, *, timeout: float = 1.0) -> bool:
    try:
        resolve_recording_rtsp_url(rtsp_url, timeout=timeout)
        return True
    except (RuntimeError, ValueError):
        return False


def should_use_rtsp_live_yolo_analysis(
    rtsp_url: str,
    *,
    timeout: float = RTSP_PUBLISH_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Return True when a live RTSP stream is publishing and can be analyzed directly."""
    return is_rtsp_stream_publishing(rtsp_url, timeout=timeout)


def clear_rtsp_publish_probe_cache() -> None:
    """Test helper: drop cached publish-probe results."""
    with _publish_probe_lock:
        _publish_probe_cache.clear()
        _publish_probe_inflight.clear()


def is_rtsp_stream_publishing(
    rtsp_url: str,
    *,
    timeout: float = RTSP_PUBLISH_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Return True when ffmpeg can decode at least one frame from the RTSP URL.

    Concurrent callers for the same URL share one in-flight probe, and recent
    results are reused briefly so UI + project-list polls do not stampede ffmpeg.
    """
    cache_key = _normalize_rtsp_url(rtsp_url.strip())
    wait_event: threading.Event | None = None
    leader = False

    with _publish_probe_lock:
        cached = _publish_probe_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < RTSP_PUBLISH_CACHE_TTL_SECONDS:
            return cached[1]

        existing = _publish_probe_inflight.get(cache_key)
        if existing is not None:
            wait_event = existing
        else:
            wait_event = threading.Event()
            _publish_probe_inflight[cache_key] = wait_event
            leader = True

    if not leader:
        assert wait_event is not None
        wait_event.wait(timeout=max(timeout, 0.1) + 0.5)
        with _publish_probe_lock:
            cached = _publish_probe_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        return False

    publishing = False
    try:
        publishing = _probe_rtsp_stream_publishing(rtsp_url, timeout=timeout)
    finally:
        with _publish_probe_lock:
            _publish_probe_cache[cache_key] = (time.monotonic(), publishing)
            _publish_probe_inflight.pop(cache_key, None)
            wait_event.set()
    return publishing


def _probe_rtsp_stream_publishing(
    rtsp_url: str,
    *,
    timeout: float = RTSP_PUBLISH_PROBE_TIMEOUT_SECONDS,
) -> bool:
    try:
        resolved_url = resolve_recording_rtsp_url(rtsp_url, timeout=min(timeout, 1.0))
    except (RuntimeError, ValueError):
        return False

    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        DEFAULT_RTSP_TRANSPORT,
        "-i",
        resolved_url,
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def resolve_recording_rtsp_url(rtsp_url: str, *, timeout: float = 1.5) -> str:
    """Verify RTSP port is reachable; retry with host.docker.internal for localhost URLs."""
    parsed = urlparse(rtsp_url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid RTSP URL: {rtsp_url}")
    port = parsed.port or 554
    first_error = _try_connect(host, port, timeout=timeout)
    if first_error is None:
        return rtsp_url

    fallback_url = ""
    fallback_error: OSError | None = None
    if host.lower() in LOCALHOST_NAMES and RTSP_LOCALHOST_FALLBACK_HOST:
        fallback_url = _replace_rtsp_host(rtsp_url, RTSP_LOCALHOST_FALLBACK_HOST)
        fallback_error = _try_connect(RTSP_LOCALHOST_FALLBACK_HOST, port, timeout=timeout)
        if fallback_error is None:
            return fallback_url

    fallback_note = f"; also tried {fallback_url}" if fallback_url else ""
    raise RuntimeError(
        f"No RTSP server is listening at {host}:{port}{fallback_note}. "
        f"Start your RTSP source before recording: {rtsp_url}"
    ) from (fallback_error or first_error)


def resolve_yolo_client_rtsp_url(rtsp_url: str) -> str:
    """Rewrite RTSP host for the YOLO process that actually opens the stream.

    Backend-in-Docker often rewrites ``localhost`` → ``host.docker.internal`` so
    in-container ffmpeg can reach MediaMTX published on the host. The YOLO
    service typically runs on the host (``YOLO_API_URL=http://host.docker.internal:8001``)
    and must open ``127.0.0.1`` instead — ``host.docker.internal`` from the host
    process fails with HTTP 502 / unable to open RTSP.
    """
    parsed = urlparse(rtsp_url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return rtsp_url

    yolo_api = (YOLO_API_URL or "").strip().lower()
    yolo_on_host = any(
        marker in yolo_api
        for marker in ("host.docker.internal", "127.0.0.1", "localhost", "[::1]")
    )
    if yolo_on_host and host in {"host.docker.internal", "gateway.docker.internal"}:
        return _replace_rtsp_host(rtsp_url, "127.0.0.1")
    return rtsp_url


def _try_connect(host: str, port: int, *, timeout: float) -> OSError | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return exc


def _replace_rtsp_host(rtsp_url: str, host: str) -> str:
    parsed = urlparse(rtsp_url)
    port = parsed.port or 554
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return parsed._replace(netloc=f"{auth}{display_host}:{port}").geturl()


def _video_encode_args(*, codec: str, live_fragmented: bool) -> list[str]:
    """Build output video codec / container flags for RTSP captures."""
    normalized = (codec or RTSP_RECORD_VIDEO_CODEC or "copy").strip().lower()
    movflags = RTSP_LIVE_MOVFLAGS if live_fragmented else RTSP_FINISHED_MOVFLAGS
    if normalized in {"copy", "streamcopy", "c_copy"}:
        return ["-an", "-c:v", "copy", "-movflags", movflags]
    return [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        movflags,
    ]


def _build_rtsp_ffmpeg_command(
    *,
    rtsp_url: str,
    output_path: Path,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    duration_sec: float | None = None,
    video_codec: str | None = None,
    live_fragmented: bool | None = None,
    rw_timeout_us: int | None = None,
) -> list[str]:
    """Build an ffmpeg command that records RTSP with timeouts and optional stream copy.

    Continuous watchdog recordings use fragmented MP4 (``live_fragmented=True``) so
    monitor segments can be cut from the growing file without opening another RTSP reader.
    """
    timeout_us = RTSP_FFMPEG_RW_TIMEOUT_US if rw_timeout_us is None else max(1, int(rw_timeout_us))
    # Default: long-running captures stay fragmented; fixed-duration clips finalize normally.
    fragmented = (duration_sec is None) if live_fragmented is None else live_fragmented
    codec = video_codec if video_codec is not None else RTSP_RECORD_VIDEO_CODEC
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rtsp_transport",
        rtsp_transport,
        # RTSP demuxer option (microseconds). Do not use ``-rw_timeout`` — unsupported
        # as a global option on common distro builds (e.g. Debian ffmpeg 7.1).
        "-timeout",
        str(timeout_us),
        "-i",
        rtsp_url,
    ]
    if duration_sec is not None:
        command.extend(["-t", f"{duration_sec:.3f}"])
    command.extend(_video_encode_args(codec=codec, live_fragmented=fragmented))
    command.append(str(output_path))
    return command


def _build_extract_clip_command(
    *,
    source_path: Path,
    output_path: Path,
    start_sec: float,
    duration_sec: float,
) -> list[str]:
    """Cut a clip from an on-disk recording using stream copy (no re-encode)."""
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{max(0.1, duration_sec):.3f}",
        "-an",
        "-c:v",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        RTSP_FINISHED_MOVFLAGS,
        str(output_path),
    ]


def extract_video_clip(
    *,
    source_path: Path,
    output_path: Path,
    start_sec: float,
    duration_sec: float,
) -> Path:
    """Extract ``[start_sec, start_sec+duration_sec)`` from ``source_path`` via ffmpeg ``-c copy``."""
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise RuntimeError(f"Source recording is missing or empty: {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    command = _build_extract_clip_command(
        source_path=source_path,
        output_path=output_path,
        start_sec=start_sec,
        duration_sec=duration_sec,
    )
    # Stream copy is normally near-instant; keep a modest wall-clock ceiling.
    timeout_sec = max(20.0, min(120.0, duration_sec + 20.0))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg clip extract timed out after {timeout_sec:.1f} seconds") from exc
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip() or "ffmpeg clip extract failed")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg clip extract produced no output: {output_path}")
    return output_path


def capture_monitor_segment_clip(
    *,
    storage_key: str,
    output_path: Path,
    segment_start_sec: float,
    duration_sec: float,
    rtsp_url: str,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
) -> Path:
    """Capture one monitor segment clip without a parallel long RTSP re-encode.

    Preferred path: cut from the active watchdog recording (same timeline).
    Fallback: short RTSP pull with ``-c copy`` and network timeouts.
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")

    last_error: Exception | None = None
    active_path: Path | None = None
    try:
        from .rtsp_watchdog import get_active_recording

        active = get_active_recording(storage_key)
        if active is not None:
            active_path = active.output_path
    except Exception as exc:  # pragma: no cover - defensive import/runtime guard
        last_error = exc
        active_path = None

    if active_path is not None and active_path.is_file() and active_path.stat().st_size > 0:
        # Growing recordings may lag slightly behind wall clock; retry a few times.
        needed_sec = max(0.0, float(segment_start_sec)) + float(duration_sec)
        for attempt in range(4):
            try:
                probed_duration, _ = probe_recorded_video(active_path)
                if probed_duration > 0 and probed_duration + 0.75 < needed_sec:
                    time.sleep(0.75)
                    continue
                return extract_video_clip(
                    source_path=active_path,
                    output_path=output_path,
                    start_sec=segment_start_sec,
                    duration_sec=duration_sec,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.75)
        # Fall through only when the window is still near the live edge.

    # Live RTSP copy can only capture "now"; never use it for historical segments.
    if float(segment_start_sec) > 2.0:
        detail = f"watchdog cut failed: {last_error}" if last_error is not None else "watchdog recording unavailable"
        raise RuntimeError(
            f"Monitor clip capture failed for historical segment "
            f"(start={segment_start_sec:.1f}s); {detail}"
        )

    try:
        recording = record_rtsp_stream(
            rtsp_url=rtsp_url,
            output_path=output_path,
            duration_sec=duration_sec,
            rtsp_transport=rtsp_transport,
            skip_server_check=True,
            video_codec="copy",
            live_fragmented=False,
        )
        return recording.playback_path
    except Exception as exc:
        if last_error is not None:
            raise RuntimeError(
                f"Monitor clip capture failed (watchdog cut: {last_error}; rtsp copy: {exc})"
            ) from exc
        raise


def _finalize_recording_result(
    *,
    rtsp_url: str,
    output_path: Path,
    playback_path: Path | None = None,
    video_start_ts: int | None = None,
    fallback_duration_sec: float = DEFAULT_RTSP_RECORD_SECONDS,
) -> RtspRecordingResult:
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"RTSP recording produced no output: {output_path}")

    from .rtsp_sei import first_pose_from_pictures
    from .rtsp_timeline import ensure_recording_frame_metadata

    pictures = ensure_recording_frame_metadata(output_path)
    pose = first_pose_from_pictures(pictures)
    probed_duration_sec, probed_fps = probe_recorded_video(output_path)
    duration_sec = probed_duration_sec if probed_duration_sec > 0 else fallback_duration_sec
    fps = probed_fps if probed_fps > 0 else 25.0
    if pose is not None:
        start_ts = pose.timestamp_ms
    elif video_start_ts is not None:
        start_ts = video_start_ts
    else:
        start_ts = _recording_start_ts(output_path)
    video_end_ts = start_ts + int(round(duration_sec * 1000))
    frame_count = len(pictures) if pictures else max(1, int(round(duration_sec * fps)))

    return RtspRecordingResult(
        rtsp_url=rtsp_url,
        output_path=output_path,
        playback_path=playback_path or output_path,
        video_start_ts=start_ts,
        video_end_ts=video_end_ts,
        duration_sec=duration_sec,
        fps=fps,
        frame_count=frame_count,
    )


def record_rtsp_stream(
    *,
    rtsp_url: str,
    output_path: Path,
    duration_sec: float = DEFAULT_RTSP_RECORD_SECONDS,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    skip_server_check: bool = False,
    video_codec: str | None = None,
    live_fragmented: bool | None = None,
) -> RtspRecordingResult:
    """Record a fixed-duration RTSP clip to disk via ffmpeg."""
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")
    if not skip_server_check:
        rtsp_url = resolve_recording_rtsp_url(rtsp_url)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    from .rtsp_timeline import resolve_recording_video_start_ts, write_recording_timeline_meta

    timeline = resolve_recording_video_start_ts(rtsp_url)
    video_start_ts = timeline.timestamp_ms
    write_recording_timeline_meta(
        output_path,
        video_start_ts=video_start_ts,
        source=timeline.source,
        rtsp_url=rtsp_url,
        x=timeline.x,
        y=timeline.y,
        yaw=timeline.yaw,
    )
    command = _build_rtsp_ffmpeg_command(
        rtsp_url=rtsp_url,
        output_path=output_path,
        rtsp_transport=rtsp_transport,
        duration_sec=duration_sec,
        video_codec=video_codec,
        live_fragmented=live_fragmented,
    )
    # Copy mode should finish near realtime; keep a smaller slack than re-encode.
    codec = (video_codec if video_codec is not None else RTSP_RECORD_VIDEO_CODEC).strip().lower()
    slack_sec = 15.0 if codec in {"copy", "streamcopy", "c_copy"} else 30.0
    timeout_sec = duration_sec + slack_sec
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg RTSP recording timed out after {timeout_sec:.1f} seconds") from exc
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip() or "ffmpeg RTSP recording failed")

    return _finalize_recording_result(
        rtsp_url=rtsp_url,
        output_path=output_path,
        video_start_ts=video_start_ts,
        fallback_duration_sec=duration_sec,
    )


def spawn_record_rtsp_until_disconnect(
    *,
    rtsp_url: str,
    output_path: Path,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    max_duration_sec: float | None = None,
) -> subprocess.Popen[str]:
    """Start a background ffmpeg process that records until the stream drops."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    command = _build_rtsp_ffmpeg_command(
        rtsp_url=rtsp_url,
        output_path=output_path,
        rtsp_transport=rtsp_transport,
        duration_sec=max_duration_sec,
        live_fragmented=True,
    )
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def record_rtsp_until_disconnect(
    *,
    rtsp_url: str,
    output_path: Path,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    skip_server_check: bool = False,
) -> RtspRecordingResult:
    if not skip_server_check:
        rtsp_url = resolve_recording_rtsp_url(rtsp_url)

    from .rtsp_timeline import resolve_recording_video_start_ts, write_recording_timeline_meta

    timeline = resolve_recording_video_start_ts(rtsp_url)
    video_start_ts = timeline.timestamp_ms
    write_recording_timeline_meta(
        output_path,
        video_start_ts=video_start_ts,
        source=timeline.source,
        rtsp_url=rtsp_url,
        x=timeline.x,
        y=timeline.y,
        yaw=timeline.yaw,
    )
    process = spawn_record_rtsp_until_disconnect(
        rtsp_url=rtsp_url,
        output_path=output_path,
        rtsp_transport=rtsp_transport,
    )
    _, stderr = process.communicate()
    if process.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(stderr.strip() or "ffmpeg RTSP recording failed")

    return _finalize_recording_result(
        rtsp_url=rtsp_url,
        output_path=output_path,
        video_start_ts=video_start_ts,
    )


def recording_result_from_path(
    *,
    rtsp_url: str,
    output_path: Path,
    playback_path: Path | None = None,
) -> RtspRecordingResult:
    return _finalize_recording_result(
        rtsp_url=rtsp_url,
        output_path=output_path,
        playback_path=playback_path,
    )


def build_rtsp_recording_path(project_id: int, *, recorded_at: datetime | None = None) -> Path:
    return build_rtsp_recording_path_for_storage_key(str(project_id), recorded_at=recorded_at)


def build_rtsp_recording_path_for_storage_key(storage_key: str, *, recorded_at: datetime | None = None) -> Path:
    timestamp = (recorded_at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return RTSP_RECORDINGS_DIR / storage_key / f"recording_{timestamp}.mp4"


def _rtsp_url_match_candidates(rtsp_url: str) -> set[str]:
    """Return normalized URL variants that should be treated as the same stream.

    Backend-in-Docker often rewrites ``localhost`` → ``host.docker.internal`` (and the
    reverse when calling host-side YOLO). Vehicle config and imported projects may use
    either form; matching must treat them as equivalent.
    """
    source = _normalize_rtsp_url(rtsp_url)
    candidates = {source}
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    if host in LOCALHOST_NAMES and RTSP_LOCALHOST_FALLBACK_HOST:
        candidates.add(_normalize_rtsp_url(_replace_rtsp_host(source, RTSP_LOCALHOST_FALLBACK_HOST)))
    elif host in {RTSP_LOCALHOST_FALLBACK_HOST.lower(), "gateway.docker.internal"}:
        candidates.add(_normalize_rtsp_url(_replace_rtsp_host(source, "127.0.0.1")))
        candidates.add(_normalize_rtsp_url(_replace_rtsp_host(source, "localhost")))
    return candidates


def resolve_storage_key_for_rtsp_url(rtsp_url: str) -> str:
    from .rtsp_vehicles import load_rtsp_vehicles

    candidates = _rtsp_url_match_candidates(rtsp_url)
    for vehicle in load_rtsp_vehicles():
        vehicle_candidates = _rtsp_url_match_candidates(vehicle.rtsp_url)
        if candidates & vehicle_candidates:
            return vehicle.id
    parsed = urlparse(rtsp_url)
    host = parsed.hostname or "unknown"
    # Canonicalize localhost aliases so storage keys stay stable across Docker/host forms.
    if host.lower() in LOCALHOST_NAMES or host.lower() in {
        RTSP_LOCALHOST_FALLBACK_HOST.lower(),
        "gateway.docker.internal",
    }:
        host = "127.0.0.1"
    path_part = (parsed.path or "/live").strip("/").replace("/", "_") or "live"
    return f"{host}_{path_part}"


def resolve_map_vehicle_id(*, vehicle_id: str | None = None, rtsp_url: str | None = None) -> str:
    """Prefer the selected vehicle id for map lookup; fall back to URL→storage_key."""
    cleaned = (vehicle_id or "").strip()
    if cleaned:
        return cleaned
    return resolve_storage_key_for_rtsp_url((rtsp_url or "").strip())


def _vehicle_map_id(vehicle_id: str | None) -> str | None:
    from .rtsp_vehicles import get_vehicle_by_id

    cleaned = (vehicle_id or "").strip()
    if not cleaned:
        return None
    vehicle = get_vehicle_by_id(cleaned)
    return vehicle.map_id if vehicle is not None else None


def resolve_project_vehicle_id(project: Project) -> str | None:
    """Read persisted vehicle id from the project (vehicle_id, then point_topic, then rtsp_summary)."""
    raw_vehicle = getattr(project, "vehicle_id", None)
    if raw_vehicle and str(raw_vehicle).strip():
        return str(raw_vehicle).strip()
    if project.point_topic and str(project.point_topic).strip() and not str(project.point_topic).startswith("/"):
        return str(project.point_topic).strip()
    if not project.artifacts_dir:
        return None
    summary_path = Path(project.artifacts_dir) / "summaries" / "rtsp_summary.json"
    if not summary_path.is_file():
        return None
    try:
        payload = read_json(summary_path)
    except (OSError, ValueError, TypeError):
        return None
    raw = payload.get("vehicle_id") if isinstance(payload, dict) else None
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def resolve_project_rtsp_url(project: Project) -> str:
    """Live YAML URL for the bound vehicle, falling back to the imported bag_dir snapshot."""
    from .rtsp_vehicles import get_vehicle_by_id

    vehicle_id = resolve_project_vehicle_id(project)
    if vehicle_id:
        vehicle = get_vehicle_by_id(vehicle_id)
        if vehicle is not None:
            return vehicle.rtsp_url.strip()
    return (project.bag_dir or "").strip()


def list_storage_key_recordings(storage_key: str) -> list[Path]:
    storage_dir = RTSP_RECORDINGS_DIR / storage_key
    if not storage_dir.is_dir():
        return []

    recordings = [
        candidate
        for candidate in storage_dir.glob("recording_*.mp4")
        if candidate.is_file() and candidate.stat().st_size > 0
    ]
    return sorted(recordings, key=lambda path: path.stat().st_mtime)


def prune_oldest_storage_key_recording_if_over_limit(
    storage_key: str,
    *,
    max_recordings: int = 5,
) -> dict[str, int]:
    recordings = list_storage_key_recordings(storage_key)
    if len(recordings) <= max_recordings:
        return {"deleted_files": 0, "freed_bytes": 0}

    oldest = recordings[0]
    try:
        freed_bytes = oldest.stat().st_size
        _unlink_recording_with_sidecars(oldest)
    except OSError:
        return {"deleted_files": 0, "freed_bytes": 0}

    storage_dir = oldest.parent
    if storage_dir.is_dir() and not any(storage_dir.iterdir()):
        storage_dir.rmdir()

    return {"deleted_files": 1, "freed_bytes": freed_bytes}


def clear_storage_key_recordings(storage_key: str) -> dict[str, int]:
    storage_dir = RTSP_RECORDINGS_DIR / storage_key
    if not storage_dir.is_dir():
        return {"deleted_files": 0, "freed_bytes": 0}

    deleted_files = 0
    freed_bytes = 0
    for recording_path in storage_dir.glob("recording_*.mp4"):
        try:
            if not recording_path.is_file():
                continue
            freed_bytes += recording_path.stat().st_size
            _unlink_recording_with_sidecars(recording_path)
            deleted_files += 1
        except OSError:
            continue

    if storage_dir.is_dir() and not any(storage_dir.iterdir()):
        storage_dir.rmdir()

    return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}


def storage_key_has_recordings(storage_key: str) -> bool:
    storage_dir = RTSP_RECORDINGS_DIR / storage_key
    if not storage_dir.is_dir():
        return False
    return any(
        candidate.is_file() and candidate.stat().st_size > 0
        for candidate in storage_dir.glob("recording_*.mp4")
    )


def find_latest_completed_recording_for_storage_key(storage_key: str) -> Path | None:
    storage_dir = RTSP_RECORDINGS_DIR / storage_key
    if not storage_dir.is_dir():
        return None

    candidates = sorted(storage_dir.glob("recording_*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def find_latest_completed_recording(rtsp_url: str) -> Path | None:
    storage_key = resolve_storage_key_for_rtsp_url(rtsp_url)
    return find_latest_completed_recording_for_storage_key(storage_key)


def _optional_recording_start_ts(recording_path: Path | None) -> int | None:
    if recording_path is None:
        return None
    try:
        return _recording_start_ts(recording_path)
    except OSError:
        # The cleanup worker may remove a recording between selection and response building.
        return None


def summarize_rtsp_playback_fields(
    rtsp_url: str,
    *,
    project_id: int | None = None,
) -> dict[str, str | bool | int | None]:
    from urllib.parse import quote

    from .rtsp_watchdog import get_active_recording, is_stable_recording_active_for_rtsp_url

    source_url = rtsp_url.strip()
    storage_key = resolve_storage_key_for_rtsp_url(source_url)
    recording_active = is_stable_recording_active_for_rtsp_url(source_url)
    stream_online = True if recording_active else is_rtsp_stream_publishing(source_url)
    latest = find_latest_completed_recording_for_storage_key(storage_key)
    active_recording = get_active_recording(storage_key) if stream_online else None

    if project_id is not None:
        live_url = f"/api/projects/{project_id}/rtsp-live"
    else:
        live_url = f"/api/rtsp-live?rtsp_url={quote(source_url, safe='')}"

    recorded_video_url = f"/api/rtsp-recordings/{storage_key}/latest" if latest is not None else None
    return {
        "recording_active": recording_active,
        "stream_online": stream_online,
        "live_url": live_url,
        "live_video_start_ts": active_recording.started_at_ms if active_recording is not None else None,
        "recorded_video_url": recorded_video_url,
        "recorded_video_start_ts": _optional_recording_start_ts(latest),
    }


def build_rtsp_playback_state(
    rtsp_url: str,
    *,
    project_id: int | None = None,
) -> dict[str, str | bool | int | None]:
    from urllib.parse import quote

    from .rtsp_watchdog import get_active_recording, is_stable_recording_active_for_rtsp_url

    source_url = rtsp_url.strip()
    if not source_url.lower().startswith("rtsp://"):
        raise ValueError(f"Expected an RTSP URL, got: {source_url}")

    storage_key = resolve_storage_key_for_rtsp_url(source_url)
    recording_active = is_stable_recording_active_for_rtsp_url(source_url)
    stream_online = True if recording_active else is_rtsp_stream_publishing(source_url)
    latest = find_latest_completed_recording_for_storage_key(storage_key)
    active_recording = get_active_recording(storage_key) if stream_online else None

    if project_id is not None:
        live_url = f"/api/projects/{project_id}/rtsp-live"
    else:
        live_url = f"/api/rtsp-live?rtsp_url={quote(source_url, safe='')}"

    recorded_video_url = f"/api/rtsp-recordings/{storage_key}/latest" if latest is not None else None

    return {
        "rtsp_url": source_url,
        "storage_key": storage_key,
        "recording_active": recording_active,
        "stream_online": stream_online,
        "live_url": live_url,
        "live_video_start_ts": active_recording.started_at_ms if active_recording is not None else None,
        "recorded_video_url": recorded_video_url,
        "recorded_video_start_ts": _optional_recording_start_ts(latest),
    }


def require_latest_completed_recording(rtsp_url: str) -> Path:
    recording_path = find_latest_completed_recording(rtsp_url)
    if recording_path is not None:
        return recording_path

    from .rtsp_watchdog import is_recording_active_for_rtsp_url

    if is_recording_active_for_rtsp_url(rtsp_url):
        raise RuntimeError(
            "RTSP 流正在后台录制中，请等待流结束后再导入或分析。"
        )
    raise RuntimeError(
        "尚未检测到可用的 RTSP 录制。请确认巡检车 RTSP 流已上线，后台会自动开始录制；流结束后即可导入。"
    )


def wait_for_completed_recording(
    rtsp_url: str,
    *,
    timeout_sec: float = 120.0,
    poll_interval_sec: float = 1.0,
) -> Path:
    """Block until the watchdog finishes recording or timeout, then return the latest file."""
    import time

    from .rtsp_watchdog import is_recording_active_for_rtsp_url

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() < deadline:
        if not is_recording_active_for_rtsp_url(rtsp_url):
            recording_path = find_latest_completed_recording(rtsp_url)
            if recording_path is not None and recording_path.is_file() and recording_path.stat().st_size > 0:
                return recording_path
        time.sleep(max(0.1, poll_interval_sec))

    recording_path = find_latest_completed_recording(rtsp_url)
    if recording_path is not None and recording_path.is_file() and recording_path.stat().st_size > 0:
        return recording_path
    raise RuntimeError("Timed out waiting for RTSP recording to finish.")


def publish_recording_to_project_artifacts(recording_path: Path, artifacts_dir: Path) -> Path:
    playback_path = artifacts_dir / "inspection.mp4"
    playback_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(recording_path, playback_path)
    from .rtsp_timeline import copy_recording_timeline_sidecars

    copy_recording_timeline_sidecars(recording_path, playback_path)
    return playback_path


def cleanup_rtsp_recordings(*, max_age_seconds: float | None = None) -> dict[str, int]:
    threshold_seconds = max_age_seconds if max_age_seconds is not None else float(RTSP_RECORDING_CLEANUP_INTERVAL_SECONDS)
    if threshold_seconds <= 0 or not RTSP_RECORDINGS_DIR.exists():
        return {"deleted_files": 0, "freed_bytes": 0}

    cutoff = time.time() - threshold_seconds
    deleted_files = 0
    freed_bytes = 0
    # Only prune finished watchdog recordings. Leave monitor_captures / monitor_llm_clips alone
    # so in-flight LLM review and evidence extraction are not disrupted.
    for recording_path in RTSP_RECORDINGS_DIR.rglob("recording_*.mp4"):
        try:
            if recording_path.stat().st_mtime >= cutoff:
                continue
            freed_bytes += recording_path.stat().st_size
            _unlink_recording_with_sidecars(recording_path)
            deleted_files += 1
        except OSError:
            continue

    for directory in sorted(RTSP_RECORDINGS_DIR.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}


def clear_rtsp_recordings() -> dict[str, int | str]:
    deleted_files = 0
    freed_bytes = 0
    if RTSP_RECORDINGS_DIR.exists():
        deleted_files = sum(1 for path in RTSP_RECORDINGS_DIR.rglob("*") if path.is_file())
        freed_bytes = path_size_bytes(RTSP_RECORDINGS_DIR)
        shutil.rmtree(RTSP_RECORDINGS_DIR)
    RTSP_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "status": "cleared",
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
    }


def start_rtsp_recording_cleanup_worker() -> None:
    global _cleanup_thread
    if not RTSP_RECORDING_CLEANUP_ENABLED:
        return
    if _cleanup_thread is not None and _cleanup_thread.is_alive():
        return

    _cleanup_stop.clear()
    _cleanup_thread = threading.Thread(
        target=_rtsp_recording_cleanup_loop,
        name="rtsp-recording-cleanup",
        daemon=True,
    )
    _cleanup_thread.start()


def stop_rtsp_recording_cleanup_worker() -> None:
    global _cleanup_thread
    _cleanup_stop.set()
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5.0)
        _cleanup_thread = None


def _rtsp_recording_cleanup_loop() -> None:
    while not _cleanup_stop.wait(RTSP_RECORDING_CLEANUP_INTERVAL_SECONDS):
        cleanup_rtsp_recordings()


def _record_and_publish_for_project(
    *,
    project_id: int,
    project_dirs: dict[str, Path],
    rtsp_url: str,
    duration_sec: float,
    rtsp_transport: str,
    skip_server_check: bool,
) -> RtspRecordingResult:
    storage_path = build_rtsp_recording_path(project_id)
    recording = record_rtsp_stream(
        rtsp_url=rtsp_url,
        output_path=storage_path,
        duration_sec=duration_sec,
        rtsp_transport=rtsp_transport,
        skip_server_check=skip_server_check,
    )
    playback_path = publish_recording_to_project_artifacts(recording.output_path, project_dirs["artifacts"])
    return replace(recording, playback_path=playback_path)


def adopt_recording_for_project(
    session: Session,
    project: Project,
    recording_path: Path,
    *,
    rtsp_url: str,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    vehicle_id: str | None = None,
) -> RtspRecordingResult:
    if project.id is None:
        raise RuntimeError("Project must be persisted before adopting RTSP recording")

    map_vehicle_id = resolve_map_vehicle_id(
        vehicle_id=vehicle_id or resolve_project_vehicle_id(project),
        rtsp_url=rtsp_url,
    )
    project_dirs = ensure_project_dirs_for(project)
    playback_path = publish_recording_to_project_artifacts(recording_path, project_dirs["artifacts"])
    recording = recording_result_from_path(
        rtsp_url=rtsp_url,
        output_path=recording_path,
        playback_path=playback_path,
    )
    artifacts = materialize_rtsp_recording(
        project_dirs,
        recording,
        requested_duration_sec=recording.duration_sec,
        rtsp_transport=rtsp_transport,
        vehicle_id=map_vehicle_id,
    )

    project.status = "indexed"
    project.bag_dir = rtsp_url
    project.point_topic = map_vehicle_id
    project.vehicle_id = map_vehicle_id
    project.bag_start_ts = recording.video_start_ts
    project.bag_end_ts = recording.video_end_ts
    project.bag_duration_ms = recording.video_end_ts - recording.video_start_ts
    project.message_count = 0
    project.calibration_required = False
    project.time_offset_ms = 0
    project.scene_path = None
    project.inspection_video_path = to_project_relative_path(project_dirs["root"], artifacts["inspection_video"])
    project.updated_at = datetime.now(timezone.utc)
    session.add(project)
    session.commit()
    session.refresh(project)
    return recording


def probe_recorded_video(video_path: Path) -> tuple[float, float]:
    ffprobe_bin = resolve_ffprobe_bin()
    if ffprobe_bin is None:
        return 0.0, 0.0

    duration_command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    fps_command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate",
        "-of",
        "json",
        str(video_path),
    ]

    duration_sec = 0.0
    fps = 0.0

    duration_result = subprocess.run(duration_command, capture_output=True, text=True)
    if duration_result.returncode == 0 and duration_result.stdout.strip():
        payload = json.loads(duration_result.stdout)
        raw_duration = payload.get("format", {}).get("duration")
        if raw_duration is not None:
            duration_sec = float(raw_duration)

    fps_result = subprocess.run(fps_command, capture_output=True, text=True)
    if fps_result.returncode == 0 and fps_result.stdout.strip():
        payload = json.loads(fps_result.stdout)
        streams = payload.get("streams") or []
        if streams:
            fps = _parse_frame_rate(str(streams[0].get("avg_frame_rate") or ""))

    return duration_sec, fps


def resolve_ffprobe_bin() -> str | None:
    configured = os.environ.get("FFPROBE_BIN", "").strip()
    if configured:
        return configured

    ffmpeg_path = Path(FFMPEG_BIN)
    if ffmpeg_path.name.lower().startswith("ffmpeg"):
        candidate = ffmpeg_path.with_name(f"ffprobe{ffmpeg_path.suffix}")
        if candidate.is_file():
            return str(candidate)

    return shutil.which("ffprobe")


def materialize_rtsp_recording(
    project_dirs: dict[str, Path],
    recording: RtspRecordingResult,
    *,
    requested_duration_sec: float | None = None,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    vehicle_id: str | None = None,
) -> dict[str, Path]:
    map_vehicle_id = resolve_map_vehicle_id(vehicle_id=vehicle_id, rtsp_url=recording.rtsp_url)
    dataset_summary = build_rtsp_dataset_summary(recording)
    video_manifest = build_rtsp_video_manifest(recording)
    rtsp_summary = {
        "source_type": "rtsp",
        "rtsp_url": recording.rtsp_url,
        "vehicle_id": map_vehicle_id,
        "map_id": _vehicle_map_id(map_vehicle_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "storage_path": str(recording.output_path),
        "playback_path": str(recording.playback_path),
        "video_path": str(recording.playback_path),
        "duration_sec": recording.duration_sec,
        "requested_duration_sec": requested_duration_sec or recording.duration_sec,
        "rtsp_transport": rtsp_transport,
        "fps": recording.fps,
        "frame_count": recording.frame_count,
        "video_start_ts": recording.video_start_ts,
        "video_end_ts": recording.video_end_ts,
    }

    dataset_summary_path = project_dirs["summaries"] / "dataset_summary.json"
    video_manifest_path = project_dirs["manifests"] / "video_manifest.json"
    rtsp_summary_path = project_dirs["summaries"] / "rtsp_summary.json"

    write_json(dataset_summary_path, dataset_summary)
    write_json(video_manifest_path, video_manifest)
    write_json(rtsp_summary_path, rtsp_summary)

    return {
        "dataset_summary": dataset_summary_path,
        "video_manifest": video_manifest_path,
        "rtsp_summary": rtsp_summary_path,
        "inspection_video": recording.playback_path,
    }


def build_rtsp_dataset_summary(recording: RtspRecordingResult) -> dict[str, int | float | str]:
    gap_ms = int(round(1000 / recording.fps)) if recording.fps > 0 else 40
    return {
        "video_start_ts": recording.video_start_ts,
        "video_end_ts": recording.video_end_ts,
        "point_start_ts": recording.video_start_ts,
        "point_end_ts": recording.video_end_ts,
        "median_video_gap_ms": gap_ms,
        "median_point_gap_ms": gap_ms,
        "time_offset_ms": 0,
        "inferred_fps": round(recording.fps, 3),
        "frame_count": recording.frame_count,
        "source_type": "rtsp_recording",
        "rtsp_url": recording.rtsp_url,
    }


def build_rtsp_video_manifest(recording: RtspRecordingResult) -> dict[str, object]:
    return {
        "videoPath": str(recording.playback_path),
        "storagePath": str(recording.output_path),
        "fps": round(recording.fps, 3),
        "frameCount": recording.frame_count,
        "startTs": recording.video_start_ts,
        "endTs": recording.video_end_ts,
        "sourceType": "rtsp_recording",
        "rtspUrl": recording.rtsp_url,
        "clips": [],
    }


def build_rtsp_placeholder_scene(recording: RtspRecordingResult) -> dict[str, object]:
    placeholder_point = [0.0, 0.0, 0.0, 0.5, 128.0, 128.0, 128.0]
    bounds = {
        "min": [-1.0, -1.0, -1.0],
        "max": [1.0, 1.0, 1.0],
    }
    trajectory: list[list[float]] = []
    trajectory_timestamps: list[int] = []
    trajectory_orientations: list[list[float]] = []

    return {
        "points": [placeholder_point],
        "full_points": [placeholder_point],
        "roof_removed_points": [placeholder_point],
        "floor_removed_points": [placeholder_point],
        "structure_points": [],
        "render_points": [placeholder_point],
        "default_point_mode": "roof_removed",
        "trajectory": trajectory,
        "trajectory_timestamps": trajectory_timestamps,
        "trajectory_orientations": trajectory_orientations,
        "bounds": bounds,
        "full_bounds": bounds,
        "roof_removed_bounds": bounds,
        "source_frame_count": 0,
        "coordinate_frame": "global",
        "source_type": "rtsp_placeholder",
        "raw_point_count": 1,
        "render_point_count": 1,
        "structure_point_count": 0,
        "colorized": True,
        "color_source": "placeholder",
        "cut_height_default": 0.0,
        "floor_cut_default": 0.0,
        "scene_quality": {
            "source_type": "rtsp_placeholder",
            "rtsp_url": recording.rtsp_url,
        },
        "selected_image_count": 0,
        "registered_image_count": 0,
        "alignment_status": "not_applicable",
        "alignment_rmse_m": None,
        "notes": [
            "Placeholder scene for RTSP-only project.",
            "3D visualization has no LiDAR data; provider analysis uses the recorded inspection.mp4.",
        ],
    }


def write_scene_json(path: Path, payload: object) -> None:
    """Compact JSON writer for large point-cloud scenes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def align_scene_timestamps_to_video(
    scene: dict[str, object],
    video_start_ts: int,
    video_end_ts: int,
    *,
    map_source: str | None = None,
) -> dict[str, object]:
    """
    Linearly remap trajectory timestamps onto the recorded video timeline.

    Clicking a trajectory point then seeks video via (ts - video_start_ts) / 1000.
    When the map already shares the RTSP timeline epoch with the video, keep the
    original timestamps (identity alignment) so absolute-time sync stays correct.
    """
    from .rtsp_timeline import scene_timeline_overlaps_video

    start_ts = int(video_start_ts)
    end_ts = int(video_end_ts)
    if end_ts <= start_ts:
        end_ts = start_ts + 1000

    timestamps_raw = scene.get("trajectory_timestamps") or []
    trajectory_raw = scene.get("trajectory") or []
    if not isinstance(timestamps_raw, list) or not timestamps_raw:
        if not isinstance(trajectory_raw, list) or not trajectory_raw:
            return scene
        scene["trajectory_timestamps"] = [start_ts, end_ts]
    elif scene_timeline_overlaps_video(scene, start_ts, end_ts):
        quality = scene.get("scene_quality")
        if not isinstance(quality, dict):
            quality = {}
            scene["scene_quality"] = quality
        quality["time_alignment"] = {
            "mode": "rtsp_timeline_shared",
            "map_start_ts": int(timestamps_raw[0]),
            "map_end_ts": int(timestamps_raw[-1]),
            "video_start_ts": start_ts,
            "video_end_ts": end_ts,
            "map_source": map_source,
        }
        notes = scene.get("notes")
        if not isinstance(notes, list):
            notes = []
        note = "轨迹时间戳与 RTSP 视频时钟同源；按绝对时间戳同步地图与视频。"
        if note not in notes:
            notes = [*notes, note]
        scene["notes"] = notes
        if map_source:
            scene.setdefault("source_type", "robot_map")
        return scene
    else:
        timestamps = [int(ts) for ts in timestamps_raw]
        map_start = timestamps[0]
        map_end = timestamps[-1]
        map_span = max(1, map_end - map_start)
        video_span = max(1, end_ts - start_ts)

        def remap(ts: int) -> int:
            return int(start_ts + (ts - map_start) / map_span * video_span)

        scene["trajectory_timestamps"] = [remap(ts) for ts in timestamps]

        hazard_zones = scene.get("hazard_zones")
        if isinstance(hazard_zones, list):
            for zone in hazard_zones:
                if not isinstance(zone, dict):
                    continue
                related = zone.get("related_pose_ts")
                if related is None:
                    continue
                try:
                    zone["related_pose_ts"] = remap(int(related))
                except (TypeError, ValueError):
                    continue

        quality = scene.get("scene_quality")
        if not isinstance(quality, dict):
            quality = {}
            scene["scene_quality"] = quality
        quality["time_alignment"] = {
            "mode": "linear_map_to_video",
            "map_start_ts": map_start,
            "map_end_ts": map_end,
            "video_start_ts": start_ts,
            "video_end_ts": end_ts,
            "map_source": map_source,
        }

    notes = scene.get("notes")
    if not isinstance(notes, list):
        notes = []
    note = "轨迹时间戳已对齐到录制视频时间轴；点击轨迹点可同步跳转视频。"
    if note not in notes:
        notes = [*notes, note]
    scene["notes"] = notes

    if map_source:
        scene.setdefault("source_type", "robot_map")
    return scene


def build_rtsp_scene_for_recording(
    recording: RtspRecordingResult,
    *,
    vehicle_id: str | None = None,
) -> dict[str, object]:
    """Load the vehicle's indexed catalog map in memory; never copy it into the project."""
    from .maps import load_map_for_vehicle_id
    from .rtsp_vehicles import is_point_cloud_enabled

    if not is_point_cloud_enabled():
        return build_rtsp_placeholder_scene(recording)

    map_vehicle_id = resolve_map_vehicle_id(vehicle_id=vehicle_id, rtsp_url=recording.rtsp_url)
    payload = load_map_for_vehicle_id(map_vehicle_id)
    if payload is None:
        return build_rtsp_placeholder_scene(recording)

    quality = payload.get("scene_quality")
    if isinstance(quality, dict) and quality.get("trajectory_source") == "rtsp_sei":
        return payload

    map_source = None
    if isinstance(quality, dict):
        map_source = quality.get("map_source") or quality.get("map_id")
    return align_scene_timestamps_to_video(
        payload,
        recording.video_start_ts,
        recording.video_end_ts,
        map_source=str(map_source) if map_source else None,
    )


def maybe_upgrade_rtsp_placeholder_scene(project: Project, scene_path: Path, payload: dict) -> dict:
    """Maps are no longer copied into the project; keep the payload unchanged."""
    return payload


def record_rtsp_for_project(
    session: Session,
    project: Project,
    *,
    rtsp_url: str | None = None,
    duration_sec: float = DEFAULT_RTSP_RECORD_SECONDS,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    skip_server_check: bool = False,
) -> RtspRecordingResult:
    if project.id is None:
        raise RuntimeError("Project must be persisted before RTSP recording")

    source_url = (rtsp_url or project.bag_dir or DEFAULT_RTSP_URL).strip()
    if not source_url.lower().startswith("rtsp://"):
        raise ValueError(f"Expected an RTSP URL, got: {source_url}")

    project.status = "rtsp_recording"
    project.updated_at = datetime.now(timezone.utc)
    session.add(project)
    session.commit()

    try:
        if not skip_server_check:
            source_url = resolve_recording_rtsp_url(source_url)
        recording_path = require_latest_completed_recording(source_url)
        return adopt_recording_for_project(
            session,
            project,
            recording_path,
            rtsp_url=source_url,
            rtsp_transport=rtsp_transport,
        )
    except Exception:
        project.status = "rtsp_failed"
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()
        raise


def import_rtsp_project(
    session: Session,
    name: str,
    rtsp_url: str,
    standards_dir: Path,
    *,
    vehicle_id: str | None = None,
    duration_sec: float = DEFAULT_RTSP_RECORD_SECONDS,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
    skip_server_check: bool = False,
) -> Project:
    """Create an RTSP-backed project, parse rules, and adopt the latest completed recording."""
    from .import_pipeline import _clear_project_records, prepare_vehicle_workspace
    from .rtsp_vehicles import get_vehicle_by_id

    source_url = rtsp_url.strip()
    if not source_url.lower().startswith("rtsp://"):
        raise ValueError(f"Expected an RTSP URL, got: {source_url}")

    selected_vehicle_id = (vehicle_id or "").strip() or None
    if selected_vehicle_id:
        vehicle = get_vehicle_by_id(selected_vehicle_id)
        if vehicle is None:
            raise ValueError(f"Unknown RTSP vehicle id: {selected_vehicle_id}")
        source_url = vehicle.rtsp_url.strip()
    else:
        # Keep URL-based fallback for API callers that omit vehicle_id.
        selected_vehicle_id = resolve_storage_key_for_rtsp_url(source_url)

    standards_dir = standards_dir.resolve()
    if not standards_dir.is_dir():
        raise FileNotFoundError(f"Standards directory not found: {standards_dir}")

    project = prepare_vehicle_workspace(
        session,
        name=name,
        bag_dir=source_url,
        standards_dir=str(standards_dir),
        vehicle_id=selected_vehicle_id,
        rtsp_vehicle=True,
    )
    project_dirs = ensure_project_dirs_for(project)
    project_id = project.id
    if project_id is None:
        raise RuntimeError("Project creation failed")

    try:
        rules = parse_rules(standards_dir, project_id)
        if not skip_server_check:
            source_url = resolve_recording_rtsp_url(source_url)
        recording_path = require_latest_completed_recording(source_url)
        recording = adopt_recording_for_project(
            session,
            project,
            recording_path,
            rtsp_url=source_url,
            rtsp_transport=rtsp_transport,
            vehicle_id=selected_vehicle_id,
        )
        rules_path = project_dirs["summaries"] / "rules.json"
        write_json(rules_path, export_rules_payload(rules))
        sync_rules_to_db(rules)

        _clear_project_records(session, project_id)
        for rule in rules:
            session.add(rule)

        project.rules_count = len(rules)
        project.findings_count = 0
        project.rules_path = str(rules_path)
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()
        session.refresh(project)
    except Exception:
        session.rollback()
        project = session.get(Project, project_id) or project
        project.status = "rtsp_failed"
        project.updated_at = datetime.now(timezone.utc)
        session.add(project)
        session.commit()
        raise

    from .rtsp_auto_analysis import schedule_auto_analysis_for_project

    if project.id is not None:
        schedule_auto_analysis_for_project(project.id)
    return project


def default_standards_dir() -> Path:
    if DEFAULT_STANDARDS_DIR.exists() and DEFAULT_STANDARDS_DIR.is_dir():
        return DEFAULT_STANDARDS_DIR
    raise FileNotFoundError(f"Default standards directory not found: {DEFAULT_STANDARDS_DIR}")


def load_rtsp_project_settings(project: Project) -> RtspProjectSettings:
    live_url = resolve_project_rtsp_url(project) or DEFAULT_RTSP_URL
    summary_path = Path(project.artifacts_dir) / "summaries" / "rtsp_summary.json" if project.artifacts_dir else None
    if summary_path is not None and summary_path.exists():
        payload = read_json(summary_path)
        storage_raw = payload.get("storage_path")
        return RtspProjectSettings(
            rtsp_url=live_url,
            duration_sec=float(payload.get("requested_duration_sec") or payload.get("duration_sec") or DEFAULT_RTSP_RECORD_SECONDS),
            rtsp_transport=str(payload.get("rtsp_transport") or DEFAULT_RTSP_TRANSPORT),
            storage_path=Path(storage_raw) if storage_raw else None,
        )
    return RtspProjectSettings(
        rtsp_url=live_url,
        duration_sec=DEFAULT_RTSP_RECORD_SECONDS,
        rtsp_transport=DEFAULT_RTSP_TRANSPORT,
        storage_path=None,
    )


def archive_current_inspection_video(project: Project, project_dirs: dict[str, Path]) -> Path | None:
    playback_path = _project_playback_path(project, project_dirs)
    if not playback_path.is_file():
        return None

    previous_path = project_dirs["artifacts"] / "previous_inspection.mp4"
    previous_meta_path = project_dirs["summaries"] / "previous_inspection.json"
    shutil.copy2(playback_path, previous_path)

    dataset_summary_path = project_dirs["summaries"] / "dataset_summary.json"
    meta: dict[str, object] = {"playback_path": str(previous_path)}
    if dataset_summary_path.exists():
        dataset_summary = read_json(dataset_summary_path)
        meta.update(
            {
                "video_start_ts": int(dataset_summary.get("video_start_ts") or project.bag_start_ts or 0),
                "video_end_ts": int(dataset_summary.get("video_end_ts") or project.bag_end_ts or 0),
                "archived_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    write_json(previous_meta_path, meta)
    return previous_path


def resolve_rtsp_analysis_videos(project: Project) -> list["AnalysisVideoTarget"]:
    from .analysis_types import AnalysisVideoTarget, dedupe_analysis_video_targets

    if project.id is None:
        raise RuntimeError("Project must be persisted before RTSP analysis")

    project_dirs = ensure_project_dirs_for(project)
    playback_path = _project_playback_path(project, project_dirs)
    previous_path = project_dirs["artifacts"] / "previous_inspection.mp4"
    previous_meta_path = project_dirs["summaries"] / "previous_inspection.json"

    targets: list[AnalysisVideoTarget] = []

    if previous_path.is_file():
        previous_meta = read_json(previous_meta_path) if previous_meta_path.exists() else {}
        targets.append(
            AnalysisVideoTarget(
                path=previous_path,
                label="previous",
                video_start_ts=int(previous_meta.get("video_start_ts") or project.bag_start_ts or 0),
                video_end_ts=int(previous_meta.get("video_end_ts") or project.bag_end_ts or 0),
            )
        )

    if playback_path.is_file():
        dataset_summary_path = project_dirs["summaries"] / "dataset_summary.json"
        dataset_summary = read_json(dataset_summary_path) if dataset_summary_path.exists() else {}
        targets.append(
            AnalysisVideoTarget(
                path=playback_path,
                label="current",
                video_start_ts=int(dataset_summary.get("video_start_ts") or project.bag_start_ts or 0),
                video_end_ts=int(dataset_summary.get("video_end_ts") or project.bag_end_ts or 0),
            )
        )

    return dedupe_analysis_video_targets(targets)


def ensure_rtsp_videos_for_analysis(
    session: Session,
    project: Project,
    *,
    record_fresh: bool = False,
) -> list["AnalysisVideoTarget"]:
    """Ensure inspection.mp4 exists for analysis, optionally triggering a fresh recording."""
    if project.id is None:
        raise RuntimeError("Project must be persisted before RTSP analysis")

    project_dirs = ensure_project_dirs_for(project)
    settings = load_rtsp_project_settings(project)
    playback_path = _project_playback_path(project, project_dirs)

    if record_fresh:
        archive_current_inspection_video(project, project_dirs)
        record_rtsp_for_project(
            session,
            project,
            rtsp_url=settings.rtsp_url,
            rtsp_transport=settings.rtsp_transport,
        )
        session.refresh(project)
    elif not playback_path.is_file():
        record_rtsp_for_project(
            session,
            project,
            rtsp_url=settings.rtsp_url,
            rtsp_transport=settings.rtsp_transport,
        )
        session.refresh(project)

    targets = resolve_rtsp_analysis_videos(project)
    if not targets:
        raise FileNotFoundError("No RTSP inspection video is available for analysis")
    return targets


def _project_playback_path(project: Project, project_dirs: dict[str, Path]) -> Path:
    root = Path(project.artifacts_dir) if project.artifacts_dir else project_dirs["root"]
    return resolve_project_path(root, project.inspection_video_path, "artifacts/inspection.mp4")


def _parse_frame_rate(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return 0.0
        return float(numerator) / denominator_value
    try:
        return float(value)
    except ValueError:
        return 0.0


def _normalize_rtsp_url(rtsp_url: str) -> str:
    return rtsp_url.strip().rstrip("/")


def _recording_lock_dir() -> Path:
    return RTSP_RECORDINGS_DIR / ".locks"


def _recording_lock_path(normalized_rtsp_url: str) -> Path:
    digest = hashlib.sha256(normalized_rtsp_url.encode("utf-8")).hexdigest()[:24]
    return _recording_lock_dir() / f"{digest}.lock"


def _read_recording_lock_pid(lock_path: Path) -> int | None:
    try:
        first_line = lock_path.read_text(encoding="utf-8").splitlines()[0].strip()
        return int(first_line)
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # On Windows, os.kill(pid, 0) always returns success because the platform
    # does not support POSIX signal-0 liveness checks.  Fall back to psutil when
    # available; otherwise trust the file-timestamp staleness check below.
    _platform = os.name
    if _platform == "nt":
        try:
            import psutil  # type: ignore[import-untyped]
        except ModuleNotFoundError:
            return True
        return psutil.pid_exists(pid)
    return True


def _is_stale_recording_lock(lock_path: Path) -> bool:
    if not lock_path.is_file():
        return True
    pid = _read_recording_lock_pid(lock_path)
    if pid is None:
        return True
    if not _pid_alive(pid):
        return True
    # Safety net: if the lock file has not been touched in over 5 minutes,
    # the recorder process is likely orphaned regardless of OS-level liveness.
    # This protects against the Windows os.kill(pid, 0) false-positive case
    # when psutil is also unavailable.
    try:
        age_sec = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    if age_sec > 300:
        return True
    return False


def acquire_stream_recording_lock(normalized_rtsp_url: str) -> Path | None:
    lock_path = _recording_lock_path(normalized_rtsp_url)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()}\n{normalized_rtsp_url}\n".encode("utf-8")
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            if _is_stale_recording_lock(lock_path):
                lock_path.unlink(missing_ok=True)
                continue
            return None
    return None


def release_stream_recording_lock(lock_path: Path | None) -> None:
    if lock_path is None or not lock_path.is_file():
        return
    if _read_recording_lock_pid(lock_path) != os.getpid():
        return
    lock_path.unlink(missing_ok=True)


def _recording_start_ts(recording_path: Path) -> int:
    from .rtsp_timeline import read_recording_timeline_meta

    meta = read_recording_timeline_meta(recording_path)
    if meta is not None and meta.timestamp_ms > 0:
        return meta.timestamp_ms

    stem = recording_path.stem
    if stem.startswith("recording_"):
        raw = stem[len("recording_") :]
        try:
            recorded_at = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return int(recorded_at.timestamp() * 1000)
        except ValueError:
            pass
    return int(recording_path.stat().st_mtime * 1000)


def _unlink_recording_with_sidecars(recording_path: Path) -> None:
    from .rtsp_timeline import delete_recording_timeline_sidecars

    delete_recording_timeline_sidecars(recording_path)
    recording_path.unlink(missing_ok=True)


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
