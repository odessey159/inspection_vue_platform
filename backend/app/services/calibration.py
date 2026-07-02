from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml


@dataclass(slots=True)
class CameraCalibration:
    rotation_matrix: np.ndarray
    translation_vector: np.ndarray
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    source_path: Path


def load_camera_calibration(path: Path) -> CameraCalibration:
    if not path.exists():
        raise FileNotFoundError(f"Calibration file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    cleaned_lines: list[str] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("%YAML:"):
            continue
        cleaned_lines.append(line.replace("!!opencv-matrix", ""))

    payload = yaml.safe_load("\n".join(cleaned_lines))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid calibration payload: {path}")

    rotation_matrix = _opencv_matrix(payload.get("rotation_matrix"), (3, 3), "rotation_matrix")
    translation_vector = _opencv_matrix(payload.get("translation_vector"), (3, 1), "translation_vector").reshape(3)
    camera_matrix = _opencv_matrix(payload.get("camera_matrix"), (3, 3), "camera_matrix")
    dist_coeffs = _opencv_matrix(payload.get("dist_coeffs"), None, "dist_coeffs").reshape(-1)

    return CameraCalibration(
        rotation_matrix=rotation_matrix,
        translation_vector=translation_vector,
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        source_path=path,
    )


def _opencv_matrix(node: object, expected_shape: tuple[int, int] | None, field_name: str) -> np.ndarray:
    if not isinstance(node, dict):
        raise ValueError(f"Calibration field {field_name} is missing or invalid")

    rows = int(node.get("rows") or 0)
    cols = int(node.get("cols") or 0)
    data = node.get("data")
    if not rows or not cols or not isinstance(data, list):
        raise ValueError(f"Calibration field {field_name} is malformed")

    matrix = np.asarray(data, dtype=np.float64).reshape(rows, cols)
    if expected_shape is not None and matrix.shape != expected_shape:
        raise ValueError(f"Calibration field {field_name} expected shape {expected_shape}, got {matrix.shape}")
    return matrix
