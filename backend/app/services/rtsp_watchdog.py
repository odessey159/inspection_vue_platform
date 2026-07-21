"""Background poll loop that records RTSP vehicles and triggers live analysis."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..settings import (
    RTSP_WATCH_ENABLED,
    RTSP_WATCH_POLL_INTERVAL_SECONDS,
    RTSP_WATCH_TEST_MAX_RECORDINGS,
    RTSP_WATCH_TEST_MAX_SECONDS,
    RTSP_WATCH_TEST_MODE,
)
from .rtsp_auto_analysis import schedule_auto_analysis_for_rtsp_url
from .rtsp_recorder import (
    acquire_stream_recording_lock,
    build_rtsp_recording_path_for_storage_key,
    is_rtsp_stream_publishing,
    prune_oldest_storage_key_recording_if_over_limit,
    release_stream_recording_lock,
    resolve_recording_rtsp_url,
    spawn_record_rtsp_until_disconnect,
)
from .rtsp_vehicles import load_rtsp_vehicles
from .rtsp_yolo_monitor import start_rtsp_yolo_monitor, stop_all_rtsp_yolo_monitors, stop_rtsp_yolo_monitor

# Ignore brand-new recorder processes in playback-state until they look stable.
# TCP-only false starts often die within ~1s and used to flicker the UI to "live".
RECORDING_STABLE_MIN_AGE_SECONDS = 1.5
RECORDING_STABLE_MIN_BYTES = 2048


_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_active_by_key: dict[str, _ActiveSession] = {}
_live_analysis_scheduled_keys: set[str] = set()
_reserving_urls: set[str] = set()
_runtime_test_mode_override: bool | None = None


def is_rtsp_watch_test_mode() -> bool:
    if _runtime_test_mode_override is not None:
        return _runtime_test_mode_override
    return RTSP_WATCH_TEST_MODE


def set_rtsp_watch_test_mode(enabled: bool) -> bool:
    global _runtime_test_mode_override
    _runtime_test_mode_override = enabled
    return is_rtsp_watch_test_mode()


def rtsp_watch_settings_payload() -> dict[str, float | bool]:
    return {
        "test_mode": is_rtsp_watch_test_mode(),
        "test_max_seconds": RTSP_WATCH_TEST_MAX_SECONDS,
    }


@dataclass
class _ActiveSession:
    storage_key: str
    rtsp_url: str
    output_path: Path
    process: subprocess.Popen[str]
    started_at: datetime
    lock_path: Path | None = None


@dataclass(frozen=True)
class ActiveRecordingInfo:
    """Public snapshot of an in-progress watchdog recording session."""

    storage_key: str
    rtsp_url: str
    output_path: Path
    started_at_ms: int


def get_active_recording(storage_key: str) -> ActiveRecordingInfo | None:
    """Return the active watchdog recording for a storage key, if any."""
    with _lock:
        session = _active_by_key.get(storage_key)
        if session is None or session.process.poll() is not None:
            return None
        from .rtsp_recorder import _recording_start_ts

        started_at_ms = _recording_start_ts(session.output_path)
        if started_at_ms <= 0:
            started_at_ms = int(session.started_at.timestamp() * 1000)
        return ActiveRecordingInfo(
            storage_key=session.storage_key,
            rtsp_url=session.rtsp_url,
            output_path=session.output_path,
            started_at_ms=started_at_ms,
        )


def is_recording_active_for_rtsp_url(rtsp_url: str) -> bool:
    """Return True when a watchdog recorder process is still alive for this URL."""
    return _find_active_session_for_rtsp_url(rtsp_url) is not None


def is_stable_recording_active_for_rtsp_url(rtsp_url: str) -> bool:
    """Like ``is_recording_active_for_rtsp_url``, but ignores brand-new / empty false starts.

    Used by playback-state so the UI does not briefly flip to live when MediaMTX is
    reachable but nothing is publishing (recorder spawns then exits immediately).
    """
    session = _find_active_session_for_rtsp_url(rtsp_url)
    if session is None:
        return False
    age_sec = (datetime.now(timezone.utc) - session.started_at).total_seconds()
    if age_sec >= RECORDING_STABLE_MIN_AGE_SECONDS:
        return True
    try:
        return session.output_path.is_file() and session.output_path.stat().st_size >= RECORDING_STABLE_MIN_BYTES
    except OSError:
        return False


def _find_active_session_for_rtsp_url(rtsp_url: str) -> _ActiveSession | None:
    from .rtsp_recorder import (
        _normalize_rtsp_url,
        _rtsp_url_match_candidates,
        resolve_storage_key_for_rtsp_url,
    )

    source_url = rtsp_url.strip()
    normalized_urls = _rtsp_url_match_candidates(source_url)
    storage_key = resolve_storage_key_for_rtsp_url(source_url)
    with _lock:
        for session in _active_by_key.values():
            if session.process.poll() is not None:
                continue
            if session.storage_key == storage_key or _normalize_rtsp_url(session.rtsp_url) in normalized_urls:
                return session
    return None


def start_rtsp_watchdog() -> None:
    """Start the daemon thread that polls configured vehicles and records live streams."""
    global _thread
    if not RTSP_WATCH_ENABLED:
        return
    if _thread is not None and _thread.is_alive():
        return

    _stop.clear()
    _thread = threading.Thread(target=_watch_loop, name="rtsp-watchdog", daemon=True)
    _thread.start()


def stop_rtsp_watchdog() -> None:
    """Stop polling, terminate active recordings, and tear down YOLO monitors."""
    global _thread
    _stop.set()
    stop_all_rtsp_yolo_monitors()
    with _lock:
        sessions = list(_active_by_key.values())
    for session in sessions:
        if session.process.poll() is None:
            session.process.terminate()
        release_stream_recording_lock(session.lock_path)
    if _thread is not None:
        _thread.join(timeout=5.0)
        _thread = None
    with _lock:
        _active_by_key.clear()
        _live_analysis_scheduled_keys.clear()
        _reserving_urls.clear()


def _schedule_live_analysis_on_connect(session: _ActiveSession) -> None:
    """Start continuous YOLO monitoring and provider_yolo analysis when RTSP connects."""
    start_rtsp_yolo_monitor(session.storage_key, session.rtsp_url)
    with _lock:
        if session.storage_key in _live_analysis_scheduled_keys:
            return
        _live_analysis_scheduled_keys.add(session.storage_key)
    schedule_auto_analysis_for_rtsp_url(session.rtsp_url)


def _monitor_recording_session(session: _ActiveSession) -> None:
    try:
        session.process.wait()
    finally:
        with _lock:
            current = _active_by_key.get(session.storage_key)
            if current is session:
                _active_by_key.pop(session.storage_key, None)
            _live_analysis_scheduled_keys.discard(session.storage_key)
        stop_rtsp_yolo_monitor(session.storage_key)
        release_stream_recording_lock(session.lock_path)


def _watch_loop() -> None:
    while not _stop.is_set():
        _poll_once()
        _stop.wait(RTSP_WATCH_POLL_INTERVAL_SECONDS)


def _poll_once() -> None:
    from .rtsp_recorder import _normalize_rtsp_url

    seen_urls: set[str] = set()
    for vehicle in load_rtsp_vehicles():
        normalized = _normalize_rtsp_url(vehicle.rtsp_url)
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        _poll_vehicle(vehicle.id, vehicle.rtsp_url)


def _poll_vehicle(storage_key: str, configured_url: str) -> None:
    """If the vehicle stream is up and not already recorded, start ffmpeg and schedule analysis."""
    with _lock:
        existing = _active_by_key.get(storage_key)
        if existing is not None:
            if existing.process.poll() is None:
                start_rtsp_yolo_monitor(existing.storage_key, existing.rtsp_url)
                return
            _active_by_key.pop(storage_key, None)
            _live_analysis_scheduled_keys.discard(storage_key)

    if is_rtsp_watch_test_mode():
        prune_oldest_storage_key_recording_if_over_limit(
            storage_key,
            max_recordings=RTSP_WATCH_TEST_MAX_RECORDINGS,
        )

    try:
        rtsp_url = resolve_recording_rtsp_url(configured_url)
    except RuntimeError:
        return

    # Port reachability is not enough: MediaMTX can be up with no publisher.
    # Require at least one decoded frame before spawning a recorder.
    if not is_rtsp_stream_publishing(rtsp_url):
        return

    from .rtsp_recorder import _normalize_rtsp_url

    normalized = _normalize_rtsp_url(rtsp_url)
    lock_path: Path | None = None
    registered = False
    try:
        with _lock:
            if normalized in _reserving_urls:
                return
            for session in _active_by_key.values():
                if _normalize_rtsp_url(session.rtsp_url) == normalized and session.process.poll() is None:
                    return
            _reserving_urls.add(normalized)

        lock_path = acquire_stream_recording_lock(normalized)
        if lock_path is None:
            return

        with _lock:
            for session in _active_by_key.values():
                if _normalize_rtsp_url(session.rtsp_url) == normalized and session.process.poll() is None:
                    return

        max_duration_sec = RTSP_WATCH_TEST_MAX_SECONDS if is_rtsp_watch_test_mode() else None
        output_path = build_rtsp_recording_path_for_storage_key(storage_key)
        process = spawn_record_rtsp_until_disconnect(
            rtsp_url=rtsp_url,
            output_path=output_path,
            max_duration_sec=max_duration_sec,
        )
        session = _ActiveSession(
            storage_key=storage_key,
            rtsp_url=rtsp_url,
            output_path=output_path,
            process=process,
            started_at=datetime.now(timezone.utc),
            lock_path=lock_path,
        )
        with _lock:
            _active_by_key[storage_key] = session
        registered = True

        threading.Thread(
            target=_monitor_recording_session,
            args=(session,),
            name=f"rtsp-recording-{storage_key}",
            daemon=True,
        ).start()
        _schedule_live_analysis_on_connect(session)
    finally:
        with _lock:
            _reserving_urls.discard(normalized)
        if lock_path is not None and not registered:
            release_stream_recording_lock(lock_path)
