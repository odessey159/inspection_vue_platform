"""Continuous YOLO segment polling while an RTSP stream is being watched.

Main live path: each segment runs YOLO, then captures a short clip for the LLM
by cutting from the watchdog long recording (fallback: lightweight RTSP ``-c copy``).
Segment clocks use the same ``video_start_ts`` origin as the long recording timeline.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..settings import (
    RTSP_YOLO_MONITOR_CAPTURE_CLIP,
    YOLO_RTSP_SEGMENT_SECONDS,
    YOLO_RTSP_TRANSPORT,
)
from .provider import provider_available
from .provider_YOLO import invoke_yolo_rtsp_segment, yolo_available
from .rtsp_recorder import DEFAULT_RTSP_TRANSPORT, capture_monitor_segment_clip
from .rtsp_yolo_llm_chain import (
    cleanup_segment_capture,
    monitor_clip_capture_dir,
    monitor_llm_enabled,
    schedule_segment_llm_review,
    should_review_segment,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_stop_by_key: dict[str, threading.Event] = {}
_thread_by_key: dict[str, threading.Thread] = {}


def start_rtsp_yolo_monitor(
    storage_key: str,
    rtsp_url: str,
    *,
    rtsp_transport: str | None = None,
) -> None:
    """Spawn a daemon thread that repeatedly calls YOLO /predict/rtsp for one stream."""
    if not yolo_available():
        logger.info("RTSP YOLO monitor skipped for %s: YOLO_API_URL is not configured", storage_key)
        return

    with _lock:
        existing = _thread_by_key.get(storage_key)
        if existing is not None and existing.is_alive():
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_monitor_loop,
            args=(storage_key, rtsp_url, rtsp_transport, stop_event),
            name=f"rtsp-yolo-monitor-{storage_key}",
            daemon=True,
        )
        _stop_by_key[storage_key] = stop_event
        _thread_by_key[storage_key] = thread
        thread.start()


def stop_rtsp_yolo_monitor(storage_key: str) -> None:
    with _lock:
        stop_event = _stop_by_key.get(storage_key)
    if stop_event is not None:
        stop_event.set()


def stop_all_rtsp_yolo_monitors() -> None:
    with _lock:
        stop_events = list(_stop_by_key.values())
    for stop_event in stop_events:
        stop_event.set()


def _resolve_timeline_origin_ms(storage_key: str, monitor_started_ms: int) -> int:
    """Anchor finding timestamps to the watchdog recording clock when available."""
    try:
        from .rtsp_watchdog import get_active_recording

        active = get_active_recording(storage_key)
    except Exception:
        active = None
    if active is not None and active.started_at_ms > 0:
        return active.started_at_ms
    return monitor_started_ms


def _capture_segment_clip(
    *,
    storage_key: str,
    rtsp_url: str,
    transport: str,
    segment_index: int,
    segment_start_sec: float,
    segment_seconds: float,
) -> Path | None:
    """Cut/fallback-capture one segment clip; return None when capture fails."""
    capture_path = monitor_clip_capture_dir(storage_key) / f"segment_{segment_index:06d}.mp4"
    try:
        return capture_monitor_segment_clip(
            storage_key=storage_key,
            output_path=capture_path,
            segment_start_sec=segment_start_sec,
            duration_sec=segment_seconds,
            rtsp_url=rtsp_url,
            rtsp_transport=transport,
        )
    except Exception:
        logger.exception(
            "RTSP YOLO monitor capture failed for %s segment=%s; continuing with YOLO-only context",
            storage_key,
            segment_index,
        )
        cleanup_segment_capture(capture_path)
        return None


def _monitor_loop(
    storage_key: str,
    rtsp_url: str,
    rtsp_transport: str | None,
    stop_event: threading.Event,
) -> None:
    """Run YOLO on consecutive RTSP segments until stop_event is set."""
    segment_seconds = max(1.0, float(YOLO_RTSP_SEGMENT_SECONDS))
    transport = (rtsp_transport or YOLO_RTSP_TRANSPORT or DEFAULT_RTSP_TRANSPORT or "tcp").strip().lower()
    if transport not in {"tcp", "udp"}:
        transport = "tcp"

    capture_enabled = RTSP_YOLO_MONITOR_CAPTURE_CLIP and monitor_llm_enabled()
    if monitor_llm_enabled():
        logger.info(
            "RTSP YOLO monitor LLM review enabled for %s (capture_clip=%s)",
            storage_key,
            capture_enabled,
        )
    elif RTSP_YOLO_MONITOR_CAPTURE_CLIP and not provider_available():
        logger.info(
            "RTSP YOLO monitor LLM review disabled for %s: vision provider is not configured",
            storage_key,
        )

    monitor_started_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    segment_index = 0
    try:
        while not stop_event.is_set():
            clip_index = f"rtsp_watch_{storage_key}_{segment_index:06d}"
            timeline_origin_ms = _resolve_timeline_origin_ms(storage_key, monitor_started_ms)

            capture_path: Path | None = None
            try:
                # YOLO RTSP samples the live edge for ``segment_seconds`` of wall time
                # (latest-frame buffer). Anchor the clip to the trailing window after the
                # call returns so evidence matches what was actually inspected.
                result = invoke_yolo_rtsp_segment(
                    rtsp_url=rtsp_url,
                    duration_sec=segment_seconds,
                    clip_index=clip_index,
                    rtsp_transport=transport,
                    segment_index=segment_index,
                    segment_start_sec=0.0,
                )

                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                segment_end_sec = max(0.0, (now_ms - timeline_origin_ms) / 1000.0)
                segment_start_sec = max(0.0, segment_end_sec - segment_seconds)
                segment_abs_start_ts = timeline_origin_ms + int(round(segment_start_sec * 1000))

                if capture_enabled:
                    capture_path = _capture_segment_clip(
                        storage_key=storage_key,
                        rtsp_url=rtsp_url,
                        transport=transport,
                        segment_index=segment_index,
                        segment_start_sec=segment_start_sec,
                        segment_seconds=segment_seconds,
                    )

                logger.info(
                    "RTSP YOLO monitor wrote segment for %s index=%s detections=%s "
                    "segment_start=%.1fs timeline_origin=%s capture=%s",
                    storage_key,
                    segment_index,
                    len(result.detections),
                    segment_start_sec,
                    timeline_origin_ms,
                    capture_path is not None,
                )

                if should_review_segment(result):
                    schedule_segment_llm_review(
                        storage_key=storage_key,
                        rtsp_url=rtsp_url,
                        segment_index=segment_index,
                        segment_start_sec=segment_start_sec,
                        segment_duration_sec=segment_seconds,
                        yolo_result=result,
                        clip_path=capture_path,
                        video_start_ts=segment_abs_start_ts,
                        timeline_origin_ms=timeline_origin_ms,
                    )
                else:
                    cleanup_segment_capture(capture_path)

                segment_index += 1
            except Exception:
                logger.exception("RTSP YOLO monitor segment failed for %s", storage_key)
                cleanup_segment_capture(capture_path)
                if stop_event.wait(min(5.0, segment_seconds)):
                    break
    finally:
        with _lock:
            if _stop_by_key.get(storage_key) is stop_event:
                _stop_by_key.pop(storage_key, None)
            current = _thread_by_key.get(storage_key)
            if current is threading.current_thread():
                _thread_by_key.pop(storage_key, None)
