"""Environment-backed settings for the standalone YOLO inference service."""

from __future__ import annotations

import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


for env_path in (
    ROOT_DIR / ".env",
    ROOT_DIR / ".env.local",
    BACKEND_DIR / ".env",
    BACKEND_DIR / ".env.local",
):
    _load_env_file(env_path)


def _env_path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default.resolve()

    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    for base in (ROOT_DIR, BACKEND_DIR, Path.cwd()):
        resolved = (base / candidate).resolve()
        if resolved.exists():
            return resolved

    return (ROOT_DIR / candidate).resolve()


YOLO_WEIGHTS_PATH = _env_path(
    "YOLO_WEIGHTS_PATH",
    BACKEND_DIR / "models" / "YOLO" / "security_check_540.pt",
)
YOLO_IMGSZ = int(os.getenv("YOLO_IMGSZ", "960"))
YOLO_CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.25"))
YOLO_SERVICE_HOST = os.getenv("YOLO_SERVICE_HOST", "127.0.0.1").strip()
YOLO_SERVICE_PORT = int(os.getenv("YOLO_SERVICE_PORT", "8001"))
YOLO_API_KEY = os.getenv("YOLO_API_KEY", "").strip()
YOLO_DETECT_PATH = os.getenv("YOLO_DETECT_PATH", "/predict/video").strip()
YOLO_RTSP_DETECT_PATH = os.getenv("YOLO_RTSP_DETECT_PATH", "/predict/rtsp").strip()
YOLO_RTSP_DEFAULT_DURATION_SEC = float(os.getenv("YOLO_RTSP_DEFAULT_DURATION_SEC", "60"))
YOLO_RTSP_MAX_DURATION_SEC = float(os.getenv("YOLO_RTSP_MAX_DURATION_SEC", "600"))
YOLO_RTSP_SEGMENT_SECONDS = float(os.getenv("YOLO_RTSP_SEGMENT_SECONDS", "10"))
YOLO_RTSP_TRANSPORT = os.getenv("YOLO_RTSP_TRANSPORT", "tcp").strip().lower()
YOLO_LOG_DIR = _env_path("YOLO_LOG_DIR", ROOT_DIR / ".runtime" / "YOLO_log")
YOLO_EXPECTED_CLASSES = int(os.getenv("YOLO_EXPECTED_CLASSES", "19"))
# quad: crop each frame into a 2x2 mosaic and run YOLO on each tile.
# full: previous whole-frame path.
YOLO_FRAME_LAYOUT = os.getenv("YOLO_FRAME_LAYOUT", "quad").strip().lower() or "quad"
YOLO_QUAD_TILE_LABELS = os.getenv("YOLO_QUAD_TILE_LABELS", "front,rear,left,right").strip()

YOLO_LOG_DIR.mkdir(parents=True, exist_ok=True)
