#!/usr/bin/env python3
"""Extract paired camera frames and lidar point clouds from a ROS 2 bag.

Output layout:
  <output_dir>/
    images/
      frame_000000.png
      ...
    pointclouds/
      frame_000000.pcd
      ...
    pairs.csv
    summary.json

Point cloud format assumption:
  AiryPoints.data uses one packed point per 26 bytes:
    float32 x
    float32 y
    float32 z
    float32 intensity
    uint16 ring
    float64 time
This layout was validated against sample packets from this bag.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BAG_DIR = REPO_ROOT / "tidepilot_data_20260324_130504"
DEFAULT_MSG_DIR = REPO_ROOT / "msg_display" / "src" / "common" / "tidepilot_msgs" / "msg"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "extracted" / "camera_lidar_pairs"
VENDOR_DIR_CANDIDATES = [REPO_ROOT / ".vendor_local", REPO_ROOT / ".vendor"]

POINT_FIELDS = ("x", "y", "z", "intensity", "ring", "time")
POINT_SIZES = (4, 4, 4, 4, 2, 8)
POINT_TYPES = ("F", "F", "F", "F", "U", "F")
EXPECTED_POINT_STEP = sum(POINT_SIZES)


@dataclass
class TimedMessage:
    timestamp_ns: int
    msg: Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract paired camera images and lidar point clouds.")
    parser.add_argument("--bag-dir", type=Path, default=DEFAULT_BAG_DIR, help="Path to rosbag2 directory.")
    parser.add_argument("--msg-dir", type=Path, default=DEFAULT_MSG_DIR, help="Directory containing custom .msg files.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory.")
    parser.add_argument("--image-topic", default="/cam/security_check", help="Camera topic name.")
    parser.add_argument("--point-topic", default="/lidar/data", help="Lidar topic name.")
    parser.add_argument("--limit", type=int, help="Optional number of pairs to export.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files.")
    parser.add_argument(
        "--png-compression",
        type=int,
        default=1,
        help="OpenCV PNG compression level 0-9. Lower is faster.",
    )
    return parser.parse_args()


def ns_to_iso8601(timestamp_ns: int) -> str:
    dt = datetime.fromtimestamp(timestamp_ns / 1e9, tz=timezone.utc).astimezone()
    return dt.isoformat()


def clean_char_array(value: Any) -> str:
    if isinstance(value, np.ndarray):
        data = value.astype(np.uint8, copy=False).tobytes()
    elif isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        return str(value)
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")


def ensure_rosbags_importable() -> tuple[Any, Any, Any, Any]:
    for candidate in reversed([path for path in VENDOR_DIR_CANDIDATES if path.exists()]):
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)

    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_types_from_msg, get_typestore

    return AnyReader, Stores, get_types_from_msg, get_typestore


def load_typestore(msg_dir: Path):
    AnyReader, Stores, get_types_from_msg, get_typestore = ensure_rosbags_importable()
    typestore = get_typestore(Stores.ROS2_HUMBLE)

    custom_types: dict[str, Any] = {}
    for path in sorted(msg_dir.glob("*.msg")):
        custom_types.update(get_types_from_msg(path.read_text(encoding="utf-8"), f"tidepilot_msgs/msg/{path.stem}"))
    typestore.register(custom_types)
    return AnyReader, typestore


def iter_topic_messages(reader: Any, topic_name: str) -> Iterator[TimedMessage]:
    connections = [conn for conn in reader.connections if conn.topic == topic_name]
    if not connections:
        available = ", ".join(sorted(info for info in reader.topics.keys()))
        raise SystemExit(f"Topic not found: {topic_name}\nAvailable topics: {available}")

    for conn, timestamp_ns, raw_data in reader.messages(connections=connections):
        yield TimedMessage(timestamp_ns=int(timestamp_ns), msg=reader.deserialize(raw_data, conn.msgtype))


def choose_nearest_lidar(image_ts: int, prev_lidar: TimedMessage | None, next_lidar: TimedMessage | None) -> TimedMessage:
    if prev_lidar is None and next_lidar is None:
        raise SystemExit("No lidar messages found.")
    if prev_lidar is None:
        return next_lidar  # type: ignore[return-value]
    if next_lidar is None:
        return prev_lidar
    if abs(prev_lidar.timestamp_ns - image_ts) <= abs(next_lidar.timestamp_ns - image_ts):
        return prev_lidar
    return next_lidar


def image_from_message(msg: Any) -> np.ndarray:
    height = int(msg.height)
    width = int(msg.width)
    step = int(msg.step)
    raw = np.asarray(msg.data, dtype=np.uint8)
    used = height * step
    raw = raw[:used]
    if step == width * 3:
        return raw.reshape(height, width, 3)

    # Fallback for padded rows.
    return raw.reshape(height, step)[:, : width * 3].reshape(height, width, 3)


def write_image(path: Path, image_bgr: np.ndarray, compress_level: int) -> None:
    image_rgb = image_bgr[:, :, ::-1]
    Image.fromarray(image_rgb, mode="RGB").save(path, format="PNG", compress_level=compress_level)


def write_pcd(path: Path, msg: Any) -> int:
    points_num = int(msg.points_num)
    point_step = int(msg.point_step)
    if point_step != EXPECTED_POINT_STEP:
        raise SystemExit(
            f"Unexpected point_step={point_step} for {path.name}. "
            f"Expected {EXPECTED_POINT_STEP} for x,y,z,intensity,ring,time layout."
        )

    raw = np.asarray(msg.data, dtype=np.uint8)
    used = points_num * point_step
    payload = raw[:used].tobytes()

    header = "\n".join(
        [
            "# .PCD v0.7 - Point Cloud Data file format",
            "VERSION 0.7",
            f"FIELDS {' '.join(POINT_FIELDS)}",
            f"SIZE {' '.join(map(str, POINT_SIZES))}",
            f"TYPE {' '.join(POINT_TYPES)}",
            "COUNT 1 1 1 1 1 1",
            f"WIDTH {points_num}",
            "HEIGHT 1",
            "VIEWPOINT 0 0 0 1 0 0 0",
            f"POINTS {points_num}",
            "DATA binary",
            "",
        ]
    ).encode("ascii")

    with path.open("wb") as fh:
        fh.write(header)
        fh.write(payload)
    return points_num


def ensure_dirs(output_dir: Path) -> tuple[Path, Path]:
    images_dir = output_dir / "images"
    clouds_dir = output_dir / "pointclouds"
    images_dir.mkdir(parents=True, exist_ok=True)
    clouds_dir.mkdir(parents=True, exist_ok=True)
    return images_dir, clouds_dir


def main() -> int:
    args = parse_args()
    bag_dir = args.bag_dir.resolve()
    msg_dir = args.msg_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not bag_dir.exists():
        raise SystemExit(f"Bag directory not found: {bag_dir}")
    if not msg_dir.exists():
        raise SystemExit(f"Custom message directory not found: {msg_dir}")

    images_dir, clouds_dir = ensure_dirs(output_dir)
    pairs_csv_path = output_dir / "pairs.csv"
    summary_path = output_dir / "summary.json"

    AnyReader, typestore = load_typestore(msg_dir)

    rows_written = 0
    delta_values_ms: list[float] = []

    with AnyReader([bag_dir], default_typestore=typestore) as reader:
        image_iter = iter_topic_messages(reader, args.image_topic)
        lidar_iter = iter_topic_messages(reader, args.point_topic)

        prev_lidar: TimedMessage | None = None
        next_lidar: TimedMessage | None = next(lidar_iter, None)

        with pairs_csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=[
                    "pair_index",
                    "image_timestamp_ns",
                    "image_timestamp",
                    "lidar_timestamp_ns",
                    "lidar_timestamp",
                    "delta_ms",
                    "image_seq",
                    "lidar_seq",
                    "image_frame_id",
                    "lidar_frame_id",
                    "image_width",
                    "image_height",
                    "points_num",
                    "image_path",
                    "pointcloud_path",
                ],
            )
            writer.writeheader()

            for pair_index, image_item in enumerate(image_iter):
                if args.limit is not None and pair_index >= args.limit:
                    break

                while next_lidar is not None and next_lidar.timestamp_ns < image_item.timestamp_ns:
                    prev_lidar = next_lidar
                    next_lidar = next(lidar_iter, None)

                lidar_item = choose_nearest_lidar(image_item.timestamp_ns, prev_lidar, next_lidar)
                delta_ms = (image_item.timestamp_ns - lidar_item.timestamp_ns) / 1e6
                delta_values_ms.append(delta_ms)

                image_filename = f"frame_{pair_index:06d}.png"
                cloud_filename = f"frame_{pair_index:06d}.pcd"
                image_path = images_dir / image_filename
                cloud_path = clouds_dir / cloud_filename

                if args.overwrite or not image_path.exists():
                    image = image_from_message(image_item.msg)
                    write_image(image_path, image, args.png_compression)

                if args.overwrite or not cloud_path.exists():
                    points_num = write_pcd(cloud_path, lidar_item.msg)
                else:
                    points_num = int(lidar_item.msg.points_num)

                writer.writerow(
                    {
                        "pair_index": pair_index,
                        "image_timestamp_ns": image_item.timestamp_ns,
                        "image_timestamp": ns_to_iso8601(image_item.timestamp_ns),
                        "lidar_timestamp_ns": lidar_item.timestamp_ns,
                        "lidar_timestamp": ns_to_iso8601(lidar_item.timestamp_ns),
                        "delta_ms": f"{delta_ms:.3f}",
                        "image_seq": int(image_item.msg.header.seq),
                        "lidar_seq": int(lidar_item.msg.header.seq),
                        "image_frame_id": clean_char_array(image_item.msg.header.frame_id),
                        "lidar_frame_id": clean_char_array(lidar_item.msg.header.frame_id),
                        "image_width": int(image_item.msg.width),
                        "image_height": int(image_item.msg.height),
                        "points_num": points_num,
                        "image_path": str(image_path.relative_to(output_dir)),
                        "pointcloud_path": str(cloud_path.relative_to(output_dir)),
                    }
                )

                rows_written += 1
                if rows_written % 100 == 0:
                    print(
                        f"exported {rows_written} pairs; latest delta_ms={delta_ms:.3f}; "
                        f"image={image_filename}; cloud={cloud_filename}",
                        flush=True,
                    )

    summary = {
        "bag_dir": str(bag_dir),
        "image_topic": args.image_topic,
        "point_topic": args.point_topic,
        "output_dir": str(output_dir),
        "pairs_exported": rows_written,
        "pairing": "nearest_lidar_timestamp",
        "image_format": "png",
        "pointcloud_format": "pcd_binary",
        "point_layout": {
            "fields": list(POINT_FIELDS),
            "sizes": list(POINT_SIZES),
            "types": list(POINT_TYPES),
            "point_step_bytes": EXPECTED_POINT_STEP,
        },
        "delta_ms": {
            "min": min(delta_values_ms) if delta_values_ms else None,
            "max": max(delta_values_ms) if delta_values_ms else None,
            "mean": (sum(delta_values_ms) / len(delta_values_ms)) if delta_values_ms else None,
            "max_abs": max((abs(x) for x in delta_values_ms), default=None),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"done: exported {rows_written} pairs to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
