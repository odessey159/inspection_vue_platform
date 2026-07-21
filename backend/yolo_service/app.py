"""FastAPI entrypoint for the standalone YOLO inference service.

Endpoints:
- POST /predict/video  — upload a clip file for frame-stream detection
- POST /predict/rtsp   — detect on one RTSP segment (call repeatedly for long streams)
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .detector import YoloVideoDetector
from .output_log import write_detection_log
from .settings import (
    YOLO_API_KEY,
    YOLO_DETECT_PATH,
    YOLO_IMGSZ,
    YOLO_LOG_DIR,
    YOLO_RTSP_DEFAULT_DURATION_SEC,
    YOLO_RTSP_DETECT_PATH,
    YOLO_RTSP_MAX_DURATION_SEC,
    YOLO_RTSP_SEGMENT_SECONDS,
    YOLO_RTSP_TRANSPORT,
    YOLO_SERVICE_HOST,
    YOLO_SERVICE_PORT,
    YOLO_WEIGHTS_PATH,
)

logger = logging.getLogger(__name__)


class DetectionPayload(BaseModel):
    class_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    time_sec: float | None = None
    bbox: list[float] = Field(default_factory=list)


class PredictVideoResponse(BaseModel):
    detections: list[DetectionPayload] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PredictRtspRequest(BaseModel):
    rtsp_url: str = Field(min_length=1)
    duration_sec: float | None = Field(default=None, gt=0.0)
    clip_index: str = "0"
    rtsp_transport: str | None = None
    segment_index: int = Field(default=0, ge=0)
    segment_start_sec: float = Field(default=0.0, ge=0.0)


class PredictRtspSegmentResponse(BaseModel):
    segment_index: int = 0
    segment_start_sec: float = 0.0
    segment_duration_sec: float = 0.0
    detections: list[DetectionPayload] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def get_detector() -> YoloVideoDetector:
    return YoloVideoDetector()


def verify_api_key(authorization: str | None = Header(default=None)) -> None:
    if not YOLO_API_KEY:
        return
    expected = f"Bearer {YOLO_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid YOLO API key")


def _resolve_rtsp_segment_duration(duration_sec: float | None) -> float:
    resolved = duration_sec if duration_sec is not None else YOLO_RTSP_SEGMENT_SECONDS
    if resolved <= 0:
        raise HTTPException(status_code=400, detail="duration_sec must be positive")
    if resolved > YOLO_RTSP_SEGMENT_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"duration_sec exceeds per-segment maximum ({YOLO_RTSP_SEGMENT_SECONDS:.0f}s). "
                "Call /predict/rtsp once per segment."
            ),
        )
    return resolved


def _build_predict_response(
    *,
    detections: list,
    notes: list[str],
    clip_index: str,
) -> PredictVideoResponse:
    response_notes = list(notes)
    response_notes.insert(0, f"clip_index={clip_index}")
    return PredictVideoResponse(
        detections=[
            DetectionPayload(
                class_name=item.class_name,
                confidence=item.confidence,
                time_sec=item.time_sec,
                bbox=item.bbox,
            )
            for item in detections
        ],
        notes=response_notes,
    )


def _build_rtsp_segment_response(
    *,
    detections: list,
    notes: list[str],
    clip_index: str,
    segment_index: int,
    segment_start_sec: float,
    segment_duration_sec: float,
) -> PredictRtspSegmentResponse:
    response_notes = list(notes)
    response_notes.insert(0, f"clip_index={clip_index}")
    response_notes.insert(1, f"segment_index={segment_index}")
    response_notes.insert(2, f"segment_start_sec={segment_start_sec:.3f}")
    response_notes.insert(3, f"segment_duration_sec={segment_duration_sec:.3f}")
    return PredictRtspSegmentResponse(
        segment_index=segment_index,
        segment_start_sec=segment_start_sec,
        segment_duration_sec=segment_duration_sec,
        detections=[
            DetectionPayload(
                class_name=item.class_name,
                confidence=item.confidence,
                time_sec=item.time_sec,
                bbox=item.bbox,
            )
            for item in detections
        ],
        notes=response_notes,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    logger.info(
        "Starting YOLO service on %s:%s with weights=%s imgsz=%s video_endpoint=%s rtsp_endpoint=%s segment_seconds=%s log_dir=%s",
        YOLO_SERVICE_HOST,
        YOLO_SERVICE_PORT,
        YOLO_WEIGHTS_PATH,
        YOLO_IMGSZ,
        YOLO_DETECT_PATH,
        YOLO_RTSP_DETECT_PATH,
        YOLO_RTSP_SEGMENT_SECONDS,
        YOLO_LOG_DIR,
    )
    get_detector()
    yield


app = FastAPI(
    title="Inspection YOLO Service",
    version="0.1.0",
    description="Ultralytics YOLO11 detect service for inspection video clips and RTSP streams.",
    lifespan=lifespan,
)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    detector = get_detector()
    return {
        "status": "ok",
        "service": "inspection-yolo",
        "weights": str(YOLO_WEIGHTS_PATH),
        "imgsz": YOLO_IMGSZ,
        "classes": detector.class_names,
        "detect_path": YOLO_DETECT_PATH,
        "rtsp_detect_path": YOLO_RTSP_DETECT_PATH,
        "rtsp_default_duration_sec": YOLO_RTSP_DEFAULT_DURATION_SEC,
        "rtsp_max_duration_sec": YOLO_RTSP_MAX_DURATION_SEC,
        "rtsp_segment_seconds": YOLO_RTSP_SEGMENT_SECONDS,
        "log_dir": str(YOLO_LOG_DIR),
    }


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "inspection-yolo", "status": "ready"}


@app.post(YOLO_DETECT_PATH, response_model=PredictVideoResponse)
async def predict_video(
    file: UploadFile = File(...),
    clip_index: str = Form(default="0"),
    _: None = Depends(verify_api_key),
) -> PredictVideoResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing uploaded video file")

    suffix = Path(file.filename).suffix or ".mp4"
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded video file is empty")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(payload)

    try:
        detector = get_detector()
        loop = asyncio.get_running_loop()
        detections, notes = await loop.run_in_executor(
            None,
            detector.detect_video,
            temp_path,
        )
        response = _build_predict_response(
            detections=detections,
            notes=notes,
            clip_index=clip_index,
        )
        log_path = write_detection_log(
            source="video",
            clip_index=clip_index,
            detections=response.detections,
            notes=response.notes,
            extra={"filename": file.filename},
        )
        logger.info("Wrote YOLO video output log: %s", log_path)
        return response
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.post(YOLO_RTSP_DETECT_PATH, response_model=PredictRtspSegmentResponse)
async def predict_rtsp(
    body: PredictRtspRequest,
    _: None = Depends(verify_api_key),
) -> PredictRtspSegmentResponse:
    rtsp_url = body.rtsp_url.strip()
    if not rtsp_url.lower().startswith("rtsp://"):
        raise HTTPException(status_code=400, detail="rtsp_url must start with rtsp://")

    segment_duration_sec = _resolve_rtsp_segment_duration(body.duration_sec)
    rtsp_transport = (body.rtsp_transport or YOLO_RTSP_TRANSPORT).strip().lower()
    if rtsp_transport not in {"tcp", "udp"}:
        raise HTTPException(status_code=400, detail="rtsp_transport must be tcp or udp")

    detector = get_detector()
    loop = asyncio.get_running_loop()
    try:
        detections, notes = await loop.run_in_executor(
            None,
            lambda: detector.detect_rtsp(
                rtsp_url,
                duration_sec=segment_duration_sec,
                rtsp_transport=rtsp_transport,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response = _build_rtsp_segment_response(
        detections=detections,
        notes=notes,
        clip_index=body.clip_index,
        segment_index=body.segment_index,
        segment_start_sec=body.segment_start_sec,
        segment_duration_sec=segment_duration_sec,
    )
    log_path = write_detection_log(
        source="rtsp",
        clip_index=body.clip_index,
        detections=response.detections,
        notes=response.notes,
        extra={
            "rtsp_url": rtsp_url,
            "segment_index": body.segment_index,
            "segment_start_sec": body.segment_start_sec,
            "segment_duration_sec": segment_duration_sec,
            "rtsp_transport": rtsp_transport,
        },
    )
    logger.info("Wrote YOLO RTSP output log: %s", log_path)
    return response
