"""Human-readable LLM review logs with an embedded JSON payload for tooling."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from ..settings import LLM_LOG_DIR

_write_lock = Lock()
_MAX_TEXT_CHARS = 200_000


def _truncate(text: str, limit: int = _MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _stringify_raw_response(raw_response: object) -> str:
    if isinstance(raw_response, str):
        return raw_response
    try:
        return json.dumps(raw_response, ensure_ascii=False, indent=2)
    except TypeError:
        return str(raw_response)


def _serialize_payload(parsed_payload: object | None) -> dict[str, Any]:
    if parsed_payload is None:
        return {}
    if hasattr(parsed_payload, "model_dump"):
        return parsed_payload.model_dump()
    if isinstance(parsed_payload, dict):
        return parsed_payload
    return {"repr": str(parsed_payload)}


def build_llm_log_text(
    *,
    source: str,
    clip_index: str,
    model: str,
    raw_response: object,
    parsed_payload: object | None = None,
    notes: list[str] | None = None,
    diagnostics: list[str] | None = None,
    prompt_section: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
    notes = notes or []
    diagnostics = diagnostics or []
    raw_text = _truncate(_stringify_raw_response(raw_response))
    parsed = _serialize_payload(parsed_payload)
    findings = parsed.get("findings") if isinstance(parsed.get("findings"), list) else []

    lines = [
        "=== LLM Review Output ===",
        f"timestamp: {timestamp}",
        f"source: {source}",
        f"clip_index: {clip_index}",
        f"model: {model}",
        f"findings_count: {len(findings)}",
    ]
    if extra:
        for key, value in extra.items():
            lines.append(f"{key}: {value}")

    lines.extend(["", "notes:"])
    if notes:
        lines.extend(f"- {note}" for note in notes)
    else:
        lines.append("- (none)")

    lines.extend(["", "diagnostics:"])
    if diagnostics:
        lines.extend(f"- {item}" for item in diagnostics)
    else:
        lines.append("- (none)")

    lines.extend(["", f"findings ({len(findings)}):"])
    if findings:
        for item in findings:
            if not isinstance(item, dict):
                lines.append(f"- {item}")
                continue
            lines.append(
                "- "
                + ", ".join(
                    [
                        f"rule_id={item.get('rule_id', '')}",
                        f"confidence={float(item.get('confidence') or 0.0):.3f}",
                        f"start={float(item.get('start_offset_sec') or 0.0):.2f}s",
                        f"end={float(item.get('end_offset_sec') or 0.0):.2f}s",
                        f"desc={str(item.get('description') or '')[:120]}",
                    ]
                )
            )
    else:
        lines.append("- (none)")

    if prompt_section.strip():
        lines.extend(["", "prompt_section:", _truncate(prompt_section.strip(), 80_000)])

    lines.extend(
        [
            "",
            "raw_response:",
            raw_text,
            "",
            "payload_json:",
            json.dumps(
                {
                    "timestamp": timestamp,
                    "source": source,
                    "clip_index": clip_index,
                    "model": model,
                    "extra": extra or {},
                    "notes": notes,
                    "diagnostics": diagnostics,
                    "prompt_section": _truncate(prompt_section, 80_000) if prompt_section else "",
                    "raw_response": raw_text,
                    "parsed": parsed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _build_log_filename(*, source: str, clip_index: str, extra: dict[str, Any] | None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_source = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in source) or "llm"
    safe_clip = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in clip_index) or "clip"
    if extra is not None and "segment_index" in extra:
        segment_index = int(extra.get("segment_index") or 0)
        return f"llm_{safe_source}_segment{segment_index:03d}_{safe_clip}_{timestamp}.log"
    return f"llm_{safe_source}_{safe_clip}_{timestamp}.log"


def write_llm_log(
    *,
    source: str,
    clip_index: str,
    model: str,
    raw_response: object,
    parsed_payload: object | None = None,
    notes: list[str] | None = None,
    diagnostics: list[str] | None = None,
    prompt_section: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write one LLM response file under LLM_LOG_DIR and return its path."""
    log_dir = LLM_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _build_log_filename(source=source, clip_index=clip_index, extra=extra)
    content = build_llm_log_text(
        source=source,
        clip_index=clip_index,
        model=model,
        raw_response=raw_response,
        parsed_payload=parsed_payload,
        notes=notes,
        diagnostics=diagnostics,
        prompt_section=prompt_section,
        extra=extra,
    )

    with _write_lock:
        log_path.write_text(content, encoding="utf-8")
    return log_path
