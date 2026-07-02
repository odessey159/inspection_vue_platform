from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

from ..settings import ROSBAG_CAMERA_LIDAR_SCRIPT, ROSBAG_MSG_DIR, ROSBAG_POSE_SCRIPT


def export_camera_lidar_pairs(
    *,
    bag_dir: Path,
    output_dir: Path,
    image_topic: str,
    point_topic: str,
    overwrite: bool = False,
) -> Path:
    summary_path = output_dir / "summary.json"
    if summary_path.exists() and (output_dir / "pairs.csv").exists():
        return output_dir

    if not ROSBAG_CAMERA_LIDAR_SCRIPT.exists():
        raise FileNotFoundError(f"Camera/lidar extractor not found: {ROSBAG_CAMERA_LIDAR_SCRIPT}")

    output_dir.mkdir(parents=True, exist_ok=True)
    module = _load_module(ROSBAG_CAMERA_LIDAR_SCRIPT, "inspection_camera_lidar_extractor")
    if hasattr(module, "VENDOR_DIR_CANDIDATES"):
        module.VENDOR_DIR_CANDIDATES = []

    argv = [
        str(ROSBAG_CAMERA_LIDAR_SCRIPT),
        "--bag-dir",
        str(bag_dir),
        "--msg-dir",
        str(ROSBAG_MSG_DIR),
        "--output-dir",
        str(output_dir),
        "--image-topic",
        image_topic,
        "--point-topic",
        point_topic,
    ]
    if overwrite:
        argv.append("--overwrite")

    exit_code = _run_module_main(module, argv)
    if exit_code != 0:
        raise RuntimeError("camera/lidar extraction failed")
    return output_dir


def export_pose_calibration(*, bag_dir: Path, output_dir: Path) -> Path:
    summary_path = output_dir / "summary.json"
    if summary_path.exists():
        return output_dir

    if not ROSBAG_POSE_SCRIPT.exists():
        raise FileNotFoundError(f"Pose extractor not found: {ROSBAG_POSE_SCRIPT}")

    output_dir.mkdir(parents=True, exist_ok=True)
    module = _load_module(ROSBAG_POSE_SCRIPT, "inspection_pose_extractor")
    if hasattr(module, "VENDOR_DIR_CANDIDATES"):
        module.VENDOR_DIR_CANDIDATES = []
    module.BAG_DIR = bag_dir
    module.MSG_DIR = ROSBAG_MSG_DIR
    module.OUTPUT_DIR = output_dir
    exit_code = _run_module_main(module, [str(ROSBAG_POSE_SCRIPT)])
    if exit_code != 0:
        raise RuntimeError("pose/calibration extraction failed")
    return output_dir


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run_module_main(module, argv: list[str]) -> int:
    with _temporary_argv(argv):
        try:
            result = module.main()
        except SystemExit as exc:
            if exc.code in (0, None):
                return 0
            raise RuntimeError(str(exc.code)) from exc
    return int(result or 0)


@contextmanager
def _temporary_argv(argv: list[str]):
    previous = sys.argv[:]
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = previous
