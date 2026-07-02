from __future__ import annotations

import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = ROOT_DIR.parent
PROJECTS_HOME = ANALYSIS_DIR.parent


def _env_path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default.resolve()
    return Path(raw_value).expanduser().resolve()


def _env_list(name: str, default: list[Path]) -> list[Path]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        values = default
    else:
        values = [Path(item.strip()).expanduser() for item in raw_value.split(",") if item.strip()]
    resolved_values: list[Path] = []
    seen: set[str] = set()
    for value in values:
        resolved = value.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        resolved_values.append(resolved)
    return resolved_values


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


APP_HOME = _env_path("APP_HOME", ROOT_DIR)
RUNTIME_DIR = _env_path("RUNTIME_DIR", APP_HOME / ".runtime")
PROJECTS_DIR = _env_path("PROJECTS_DIR", RUNTIME_DIR / "projects")
DATABASE_PATH = _env_path("DATABASE_PATH", RUNTIME_DIR / "inspection.db")
INPUTS_DIR = _env_path("INPUTS_DIR", APP_HOME / "inputs")
CONFIG_DIR = _env_path("CONFIG_DIR", APP_HOME / "config")
ROSENV_DIR = _env_path("ROSENV_DIR", BACKEND_DIR / "rosenv")
DEFAULT_STANDARDS_DIR = _env_path("DEFAULT_STANDARDS_DIR", INPUTS_DIR / "standards")
SECURITY_CHECK_CALIBRATION_PATH = _env_path(
    "SECURITY_CHECK_CALIBRATION_PATH",
    CONFIG_DIR / "security_check.yaml",
)

SUPPORTED_VISION_MODELS = [
    "qwen3.5-122b-a10b",
    "qwen3.5-flash",
    "qwen3.5-35b-a3b",
    "qwen3.5-plus",
    "qwen3.5-plus-2026-02-15",
    "glm-5",
    "qwen3-max-2026-01-23",
    "qwen3.5-27b",
    "qwen3.6-plus-2026-04-02",
    "qwen3.6-plus",
    "qwen3-vl-flash-2026-01-22",
    "kimi-k2.5",
    "qwen3.5-flash-2026-02-23",
    "qwen3.5-397b-a17b",
    "qwen-flash-character-2026-02-26",
    "qwen3-coder-next",
    "qwen-flash-character",
]

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
COLMAP_BIN = os.getenv("COLMAP_BIN", "").strip()
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "dashscope").strip().lower()
VISION_API_URL = os.getenv("VISION_API_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip()
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3.5-plus").strip()
VISION_CLIP_SECONDS = int(os.getenv("VISION_CLIP_SECONDS", "25"))
VISION_VIDEO_FPS = float(os.getenv("VISION_VIDEO_FPS", "1"))
VISION_MAX_CLIP_BYTES = int(os.getenv("VISION_MAX_CLIP_BYTES", str(7 * 1024 * 1024)))
VISION_MAX_FINDINGS_PER_CLIP = int(os.getenv("VISION_MAX_FINDINGS_PER_CLIP", "4"))
VISION_MAX_RETRIES = int(os.getenv("VISION_MAX_RETRIES", "2"))
VISION_REQUEST_TIMEOUT_SECONDS = int(os.getenv("VISION_REQUEST_TIMEOUT_SECONDS", "120"))
VISION_TEMPERATURE = float(os.getenv("VISION_TEMPERATURE", "0.1"))
VISION_ENABLE_THINKING = os.getenv("VISION_ENABLE_THINKING", "false").strip().lower() in {"1", "true", "yes", "on"}

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
]

DISCOVERY_ROOTS = _env_list("DISCOVERY_ROOTS", [INPUTS_DIR, APP_HOME])


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


ROSBAG_TOOLS_DIR = ROSENV_DIR / "tools"
ROSBAG_CAMERA_LIDAR_SCRIPT = ROSBAG_TOOLS_DIR / "extract_camera_lidar_pairs.py"
ROSBAG_POSE_SCRIPT = ROSBAG_TOOLS_DIR / "extract_pose_calibration.py"
ROSBAG_MSG_DIR = ROSENV_DIR / "msg_display" / "src" / "common" / "tidepilot_msgs" / "msg"

SCENE_VOXEL_SIZE = float(os.getenv("SCENE_VOXEL_SIZE", "0.08"))
SCENE_MAX_POINTS = int(os.getenv("SCENE_MAX_POINTS", "600000"))
SCENE_MAX_SOURCE_FRAMES = int(os.getenv("SCENE_MAX_SOURCE_FRAMES", "0"))
SCENE_MAX_TRAJECTORY_SAMPLES = int(os.getenv("SCENE_MAX_TRAJECTORY_SAMPLES", "1500"))
SCENE_ROOF_QUANTILE = float(os.getenv("SCENE_ROOF_QUANTILE", "0.92"))
SCENE_FLOOR_CUT_QUANTILE = float(os.getenv("SCENE_FLOOR_CUT_QUANTILE", "0.35"))
SCENE_FLOOR_CUT_MIN_LIFT = float(os.getenv("SCENE_FLOOR_CUT_MIN_LIFT", "0.45"))
SCENE_FLOOR_CUT_MAX_LIFT = float(os.getenv("SCENE_FLOOR_CUT_MAX_LIFT", "0.72"))
SCENE_LOW_OUTLIER_QUANTILE = float(os.getenv("SCENE_LOW_OUTLIER_QUANTILE", "0.01"))
SCENE_HIGH_OUTLIER_QUANTILE = float(os.getenv("SCENE_HIGH_OUTLIER_QUANTILE", "0.995"))
SCENE_RENDER_TARGET_POINTS = int(os.getenv("SCENE_RENDER_TARGET_POINTS", "600000"))
SCENE_RENDER_MAX_POINTS = int(os.getenv("SCENE_RENDER_MAX_POINTS", "600000"))
SCENE_CALIBRATION_SAMPLE_FRAMES = int(os.getenv("SCENE_CALIBRATION_SAMPLE_FRAMES", "6"))
SCENE_CALIBRATION_SAMPLE_POINTS = int(os.getenv("SCENE_CALIBRATION_SAMPLE_POINTS", "1600"))
SCENE_EGO_FILTER_ENABLED = _env_bool("SCENE_EGO_FILTER_ENABLED", "true")
SCENE_EGO_FILTER_SAMPLE_FRAMES = int(os.getenv("SCENE_EGO_FILTER_SAMPLE_FRAMES", "160"))
SCENE_EGO_FILTER_VOXEL_SIZE = float(os.getenv("SCENE_EGO_FILTER_VOXEL_SIZE", str(SCENE_VOXEL_SIZE)))
SCENE_EGO_FILTER_MIN_HIT_RATIO = float(os.getenv("SCENE_EGO_FILTER_MIN_HIT_RATIO", "0.08"))
SCENE_EGO_FILTER_EXPAND_CELLS = int(os.getenv("SCENE_EGO_FILTER_EXPAND_CELLS", "1"))
SCENE_EGO_FILTER_MIN_X = float(os.getenv("SCENE_EGO_FILTER_MIN_X", "-1.55"))
SCENE_EGO_FILTER_MAX_X = float(os.getenv("SCENE_EGO_FILTER_MAX_X", "1.55"))
SCENE_EGO_FILTER_MIN_Y = float(os.getenv("SCENE_EGO_FILTER_MIN_Y", "-1.05"))
SCENE_EGO_FILTER_MAX_Y = float(os.getenv("SCENE_EGO_FILTER_MAX_Y", "1.05"))
SCENE_EGO_FILTER_MIN_Z = float(os.getenv("SCENE_EGO_FILTER_MIN_Z", "-0.45"))
SCENE_EGO_FILTER_MAX_Z = float(os.getenv("SCENE_EGO_FILTER_MAX_Z", "1.35"))
SCENE_EGO_SWEEP_FILTER_ENABLED = _env_bool("SCENE_EGO_SWEEP_FILTER_ENABLED", "true")
SCENE_EGO_SWEEP_RADIUS = float(os.getenv("SCENE_EGO_SWEEP_RADIUS", "0.72"))
SCENE_EGO_SWEEP_MIN_Z_OFFSET = float(os.getenv("SCENE_EGO_SWEEP_MIN_Z_OFFSET", "-0.35"))
SCENE_EGO_SWEEP_MAX_Z_OFFSET = float(os.getenv("SCENE_EGO_SWEEP_MAX_Z_OFFSET", "1.15"))
SCENE_EGO_SWEEP_SAMPLE_COUNT = int(os.getenv("SCENE_EGO_SWEEP_SAMPLE_COUNT", "1200"))
CLIP_LENGTH_SECONDS = int(os.getenv("CLIP_LENGTH_SECONDS", "25"))


RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)


def resolve_vision_model(model_override: str | None = None) -> str:
    chosen = (model_override or "").strip() or VISION_MODEL
    return chosen


def is_supported_vision_model(model: str | None) -> bool:
    if not model:
        return False
    return model in SUPPORTED_VISION_MODELS
