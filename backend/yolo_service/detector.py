"""Ultralytics YOLO wrapper for uploaded videos and live RTSP segments."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import cv2

from .settings import YOLO_CONFIDENCE, YOLO_EXPECTED_CLASSES, YOLO_IMGSZ, YOLO_WEIGHTS_PATH

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoDetection:
    class_name: str
    confidence: float
    time_sec: float
    bbox: list[float]


class YoloVideoDetector:
    def __init__(self) -> None:
        self._lock = Lock()
        self._model = self._load_model()

    def _load_model(self):
        if not YOLO_WEIGHTS_PATH.exists():
            raise FileNotFoundError(f"YOLO weights not found: {YOLO_WEIGHTS_PATH}")

        try:
            from ultralytics import YOLO
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "ultralytics is not installed. Install backend/requirements-yolo.txt first."
            ) from exc

        model = YOLO(str(YOLO_WEIGHTS_PATH))
        class_count = len(getattr(model, "names", {}) or {})
        if class_count and class_count != YOLO_EXPECTED_CLASSES:
            logger.warning(
                "YOLO model reports %s classes; expected %s",
                class_count,
                YOLO_EXPECTED_CLASSES,
            )
        logger.info(
            "Loaded YOLO weights from %s (classes=%s, imgsz=%s)",
            YOLO_WEIGHTS_PATH,
            class_count,
            YOLO_IMGSZ,
        )
        return model

    @property
    def class_names(self) -> dict[int, str]:
        names = getattr(self._model, "names", {}) or {}
        return {int(key): str(value) for key, value in names.items()}

    def detect_video(self, video_path: Path) -> tuple[list[VideoDetection], list[str]]:
        """Run streaming predict over an on-disk video file."""
        with self._lock:
            return self._detect_video_unlocked(video_path)

    def detect_rtsp(
        self,
        rtsp_url: str,
        *,
        duration_sec: float,
        rtsp_transport: str = "tcp",
    ) -> tuple[list[VideoDetection], list[str]]:
        """Capture RTSP frames for up to duration_sec and predict each frame."""
        with self._lock:
            return self._detect_rtsp_unlocked(
                rtsp_url,
                duration_sec=duration_sec,
                rtsp_transport=rtsp_transport,
            )

    def _detect_video_unlocked(self, video_path: Path) -> tuple[list[VideoDetection], list[str]]:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open uploaded video: {video_path.name}")

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        capture.release()
        if fps <= 0:
            fps = 25.0

        detections: list[VideoDetection] = []
        notes = [
            f"weights={YOLO_WEIGHTS_PATH.name}",
            f"imgsz={YOLO_IMGSZ}",
            f"video_fps={fps:.3f}",
        ]

        results = self._model.predict(
            source=str(video_path),
            imgsz=YOLO_IMGSZ,
            conf=YOLO_CONFIDENCE,
            stream=True,
            verbose=False,
        )

        frame_count = 0
        for frame_idx, result in enumerate(results):
            frame_count = frame_idx + 1
            time_sec = frame_idx / fps
            self._collect_detections(result, detections, time_sec=time_sec)

        notes.append(f"frames={frame_count}")
        notes.append(f"detections={len(detections)}")
        return detections, notes

    def _detect_rtsp_unlocked(
        self,
        rtsp_url: str,
        *,
        duration_sec: float,
        rtsp_transport: str,
    ) -> tuple[list[VideoDetection], list[str]]:
        normalized_url = rtsp_url.strip()
        if not normalized_url.lower().startswith("rtsp://"):
            raise ValueError(f"Expected an RTSP URL, got: {normalized_url}")

        transport = (rtsp_transport or "tcp").strip().lower()
        if transport not in {"tcp", "udp"}:
            raise ValueError(f"Unsupported RTSP transport: {rtsp_transport}")

        self._configure_rtsp_transport(transport)
        capture = cv2.VideoCapture(normalized_url, cv2.CAP_FFMPEG)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open RTSP stream: {normalized_url}")

        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0:
            fps = 25.0

        detections: list[VideoDetection] = []
        notes = [
            f"weights={YOLO_WEIGHTS_PATH.name}",
            f"imgsz={YOLO_IMGSZ}",
            f"source=rtsp",
            f"rtsp_transport={transport}",
            f"duration_sec={duration_sec:.3f}",
            f"stream_fps={fps:.3f}",
        ]

        started_at = time.monotonic()
        frame_count = 0
        try:
            while True:
                elapsed_sec = time.monotonic() - started_at
                if elapsed_sec >= duration_sec:
                    break

                ok, frame = capture.read()
                if not ok or frame is None:
                    break

                results = self._model.predict(
                    source=frame,
                    imgsz=YOLO_IMGSZ,
                    conf=YOLO_CONFIDENCE,
                    verbose=False,
                )
                if results:
                    self._collect_detections(results[0], detections, time_sec=elapsed_sec)
                frame_count += 1
        finally:
            capture.release()

        notes.append(f"frames={frame_count}")
        notes.append(f"detections={len(detections)}")
        return detections, notes

    def _configure_rtsp_transport(self, transport: str) -> None:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}"

    def _collect_detections(
        self,
        result: object,
        detections: list[VideoDetection],
        *,
        time_sec: float,
    ) -> None:
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return

        names = getattr(result, "names", None) or self._model.names or {}
        for box in boxes:
            cls_id = int(box.cls[0])
            class_name = str(names.get(cls_id, cls_id)).strip()
            confidence = float(box.conf[0])
            if confidence < YOLO_CONFIDENCE or not class_name:
                continue
            detections.append(
                VideoDetection(
                    class_name=class_name,
                    confidence=max(0.0, min(1.0, confidence)),
                    time_sec=time_sec,
                    bbox=[float(value) for value in box.xyxy[0].tolist()],
                )
            )
