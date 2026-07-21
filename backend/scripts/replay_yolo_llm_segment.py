"""Replay an existing YOLO log + RTSP recording segment through the monitor LLM chain.

Example:
  python backend/scripts/replay_yolo_llm_segment.py ^
    --yolo-log .runtime/YOLO_log/rtsp_segment000_rtsp_watch_local-demo_000000_20260709T114336156104Z.log ^
    --recording .runtime/rtsp_recordings/local-demo/recording_20260709T114237Z.mp4 ^
    --project-id 15 ^
    --skip-persist
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models import HazardRule, Project  # noqa: E402
from app.services.provider_YOLO import YoloClipResult, YoloDetectionPayload  # noqa: E402
from app.services import rtsp_yolo_llm_chain  # noqa: E402
from app.settings import FFMPEG_BIN, LLM_LOG_DIR  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
from app.db import engine  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yolo-log", type=Path, required=True, help="Path to a YOLO_log *.log file")
    parser.add_argument("--recording", type=Path, required=True, help="Matching RTSP recording mp4")
    parser.add_argument("--project-id", type=int, required=True, help="RTSP project used for rules/context")
    parser.add_argument("--storage-key", default="local-demo")
    parser.add_argument("--skip-persist", action="store_true", help="Do not write findings to DB")
    return parser.parse_args()


def _load_yolo_payload(log_path: Path) -> dict:
    text = log_path.read_text(encoding="utf-8")
    marker = "payload_json:"
    idx = text.find(marker)
    if idx < 0:
        raise RuntimeError(f"No payload_json section in {log_path}")
    payload = json.loads(text[idx + len(marker) :].strip())
    if not isinstance(payload, dict):
        raise RuntimeError("YOLO payload_json root must be an object")
    return payload


def _cut_segment(recording: Path, start_sec: float, duration_sec: float, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG_BIN,
        "-y",
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-i",
        str(recording),
        "-t",
        f"{max(1.0, duration_sec):.3f}",
        "-c",
        "copy",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
        # Re-encode fallback when stream copy fails on keyframe boundaries.
        command = [
            FFMPEG_BIN,
            "-y",
            "-ss",
            f"{max(0.0, start_sec):.3f}",
            "-i",
            str(recording),
            "-t",
            f"{max(1.0, duration_sec):.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            str(output_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not output_path.exists():
            raise RuntimeError(f"ffmpeg cut failed: {result.stderr.strip() or result.stdout.strip()}")
    return output_path


def main() -> int:
    args = _parse_args()
    yolo_payload = _load_yolo_payload(args.yolo_log.resolve())
    extra = yolo_payload.get("extra") if isinstance(yolo_payload.get("extra"), dict) else {}
    detections = [
        YoloDetectionPayload.model_validate(item)
        for item in (yolo_payload.get("detections") or [])
        if isinstance(item, dict)
    ]
    notes = [str(item) for item in (yolo_payload.get("notes") or [])]
    segment_index = int(extra.get("segment_index") or 0)
    segment_start_sec = float(extra.get("segment_start_sec") or 0.0)
    segment_duration_sec = float(extra.get("segment_duration_sec") or 30.0)
    rtsp_url = str(extra.get("rtsp_url") or "rtsp://127.0.0.1:18554/live")

    with Session(engine) as session:
        project = session.get(Project, args.project_id)
        if project is None:
            raise RuntimeError(f"Project {args.project_id} not found")
        rules = list(
            session.exec(
                select(HazardRule)
                .where(HazardRule.project_id == args.project_id)
                .where(HazardRule.visual_detectable == True)  # noqa: E712
            )
        )
        if not rules:
            raise RuntimeError(f"Project {args.project_id} has no visual rules")
        detached_project = Project.model_validate(project.model_dump())
        detached_rules = [HazardRule.model_validate(rule.model_dump()) for rule in rules]

    recording = args.recording.resolve()
    if not recording.is_file():
        raise RuntimeError(f"Recording not found: {recording}")

    with tempfile.TemporaryDirectory(prefix="replay-yolo-llm-") as tmp_dir:
        clip_path = _cut_segment(
            recording,
            segment_start_sec,
            segment_duration_sec,
            Path(tmp_dir) / f"segment_{segment_index:06d}.mp4",
        )
        print(f"Cut clip: {clip_path} ({clip_path.stat().st_size} bytes)")
        print(f"YOLO detections: {len(detections)}")
        print(f"LLM_LOG_DIR: {LLM_LOG_DIR}")

        yolo_result = YoloClipResult(detections=detections, notes=notes, raw_payload=yolo_payload)

        def _load_fixed_project(state: rtsp_yolo_llm_chain.SegmentReviewState):
            state.projects = [detached_project]
            state.rules = detached_rules
            state.rules_by_id = {rule.rule_id: rule for rule in detached_rules}
            state.notes.append(
                f"Replay using project-{detached_project.id} with {len(detached_rules)} visual rule(s)."
            )
            return state

        def _noop_persist(state: rtsp_yolo_llm_chain.SegmentReviewState):
            state.notes.append("skip-persist: findings were not written to DB")
            return state

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "app.services.rtsp_yolo_llm_chain._step_load_projects_and_rules",
                    side_effect=_load_fixed_project,
                )
            )
            if args.skip_persist:
                stack.enter_context(
                    patch(
                        "app.services.rtsp_yolo_llm_chain._step_persist_findings",
                        side_effect=_noop_persist,
                    )
                )
            state = rtsp_yolo_llm_chain.run_segment_llm_review(
                storage_key=args.storage_key,
                rtsp_url=rtsp_url,
                segment_index=segment_index,
                segment_start_sec=segment_start_sec,
                segment_duration_sec=segment_duration_sec,
                yolo_result=yolo_result,
                clip_path=clip_path,
            )

    finding_count = sum(len(seeds) for seeds in state.seeds_by_project.values())
    print(f"skipped={state.skipped}")
    print(f"findings={finding_count}")
    print(f"model={state.selected_model}")
    if state.provider_payload is not None:
        print("provider_payload:")
        print(json.dumps(state.provider_payload.model_dump(), ensure_ascii=False, indent=2))
    if state.notes:
        print("notes:")
        for note in state.notes[-12:]:
            print(f"  - {note}")
    if state.diagnostics:
        print("diagnostics:")
        for item in state.diagnostics:
            print(f"  - {item}")

    latest_logs = sorted(LLM_LOG_DIR.glob("llm_*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    if latest_logs:
        print(f"latest_llm_log={latest_logs[0]}")
    return 0 if not state.skipped and state.provider_payload is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
