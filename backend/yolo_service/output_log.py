"""Human-readable YOLO detection logs with an embedded JSON payload for tooling."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from .settings import YOLO_LOG_DIR

_write_lock = Lock()


def _serialize_detection(item: object) -> dict[str, object]:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if is_dataclass(item):
        return asdict(item)
    if isinstance(item, dict):
        return item
    return {
        "class_name": getattr(item, "class_name", ""),
        "confidence": getattr(item, "confidence", 0.0),
        "time_sec": getattr(item, "time_sec", None),
        "bbox": list(getattr(item, "bbox", []) or []),
    }


def _format_detection_line(detection: dict[str, object]) -> str:
    parts = [
        f"class={detection.get('class_name', '')}",
        f"confidence={float(detection.get('confidence') or 0.0):.3f}",
    ]
    time_sec = detection.get("time_sec")
    if time_sec is not None:
        parts.append(f"time_sec={float(time_sec):.3f}")
    bbox = detection.get("bbox")
    if isinstance(bbox, list) and bbox:
        bbox_text = ", ".join(f"{float(value):.1f}" for value in bbox[:4])
        parts.append(f"bbox=[{bbox_text}]")
    return "- " + ", ".join(parts)


def build_detection_log_text(
    *,
    source: str,
    clip_index: str,
    detections: list[object],
    notes: list[str],
    extra: dict[str, object] | None = None,
) -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
    serialized = [_serialize_detection(item) for item in detections]
    lines = [
        "=== YOLO Detection Output ===",
        f"timestamp: {timestamp}",
        f"source: {source}",
        f"clip_index: {clip_index}",
    ]
    if extra:
        for key, value in extra.items():
            lines.append(f"{key}: {value}")

    lines.extend(["", "notes:"])
    if notes:
        lines.extend(f"- {note}" for note in notes)
    else:
        lines.append("- (none)")

    lines.extend(["", f"detections ({len(serialized)}):"])
    if serialized:
        lines.extend(_format_detection_line(item) for item in serialized)
    else:
        lines.append("- (none)")

    lines.extend(
        [
            "",
            "payload_json:",
            json.dumps(
                {
                    "timestamp": timestamp,
                    "source": source,
                    "clip_index": clip_index,
                    "extra": extra or {},
                    "notes": notes,
                    "detections": serialized,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _build_log_filename(*, source: str, clip_index: str, extra: dict[str, object] | None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_clip = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in clip_index) or "clip"
    if source == "rtsp" and extra is not None:
        segment_index = extra.get("segment_index", 0)
        return f"rtsp_segment{int(segment_index):03d}_{safe_clip}_{timestamp}.log"
    return f"video_{safe_clip}_{timestamp}.log"


def write_detection_log(
    *,
    source: str,
    clip_index: str,
    detections: list[object],
    notes: list[str],
    extra: dict[str, object] | None = None,
) -> Path:
    """Write one detection result file under YOLO_LOG_DIR and return its path."""
    log_dir = YOLO_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _build_log_filename(source=source, clip_index=clip_index, extra=extra)
    content = build_detection_log_text(
        source=source,
        clip_index=clip_index,
        detections=detections,
        notes=notes,
        extra=extra,
    )

    with _write_lock:
        log_path.write_text(content, encoding="utf-8")
    return log_path
