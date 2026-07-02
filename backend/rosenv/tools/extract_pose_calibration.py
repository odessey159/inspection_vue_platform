#!/usr/bin/env python3
"""Export pose, TF, and calibration-related data from the ROS 2 bag."""

from __future__ import annotations

import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
BAG_DIR = REPO_ROOT / "tidepilot_data_20260324_130504"
MSG_DIR = REPO_ROOT / "msg_display" / "src" / "common" / "tidepilot_msgs" / "msg"
OUTPUT_DIR = REPO_ROOT / "extracted" / "pose_calibration"
VENDOR_DIR_CANDIDATES = [REPO_ROOT / ".vendor_local", REPO_ROOT / ".vendor"]


@dataclass
class TopicExportSpec:
    topic: str
    output_name: str
    kind: str


ODOM_TOPICS = [
    TopicExportSpec("/fusion_odom", "fusion_odom.csv", "odom"),
    TopicExportSpec("/origin_odom", "origin_odom.csv", "odom"),
]

POSE_TOPICS = [
    TopicExportSpec("/lidar_slam_pose", "lidar_slam_pose.csv", "pose_stamped"),
]

TF_TOPICS = [
    TopicExportSpec("/tf", "tf_dynamic.csv", "tf"),
    TopicExportSpec("/tf_static", "tf_static.csv", "tf"),
]

META_TOPICS = {
    "/map_name": "map_name.txt",
    "/slam_map_name": "slam_map_name.txt",
}

CAMERA_INFO_TOPIC = "/cam/security_check/info"


def ensure_rosbags_importable() -> tuple[Any, Any, Any, Any]:
    for candidate in reversed([path for path in VENDOR_DIR_CANDIDATES if path.exists()]):
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)

    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_types_from_msg, get_typestore

    return AnyReader, Stores, get_types_from_msg, get_typestore


def load_reader_and_typestore():
    AnyReader, Stores, get_types_from_msg, get_typestore = ensure_rosbags_importable()
    typestore = get_typestore(Stores.ROS2_HUMBLE)

    custom_types: dict[str, Any] = {}
    for path in sorted(MSG_DIR.glob("*.msg")):
        custom_types.update(get_types_from_msg(path.read_text(encoding="utf-8"), f"tidepilot_msgs/msg/{path.stem}"))
    typestore.register(custom_types)
    return AnyReader, typestore


def ns_to_iso8601(timestamp_ns: int) -> str:
    return datetime.fromtimestamp(timestamp_ns / 1e9, tz=timezone.utc).astimezone().isoformat()


def time_to_ns(time_msg: Any) -> int:
    return int(time_msg.sec) * 1_000_000_000 + int(time_msg.nanosec)


def yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def clean_char_array(value: Any) -> str:
    if isinstance(value, np.ndarray):
        data = value.astype(np.uint8, copy=False).tobytes()
    elif isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        return str(value)
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")


def iter_topic_messages(reader: Any, topic_name: str) -> Iterable[tuple[int, Any]]:
    connections = [conn for conn in reader.connections if conn.topic == topic_name]
    for conn, timestamp_ns, raw_data in reader.messages(connections=connections):
        yield int(timestamp_ns), reader.deserialize(raw_data, conn.msgtype)


def export_odom(reader: Any, topic: str, output_path: Path) -> dict[str, Any]:
    rows = 0
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "bag_timestamp_ns",
                "bag_timestamp",
                "header_timestamp_ns",
                "header_timestamp",
                "frame_id",
                "child_frame_id",
                "x",
                "y",
                "z",
                "qx",
                "qy",
                "qz",
                "qw",
                "yaw_rad",
                "linear_x",
                "linear_y",
                "linear_z",
                "angular_x",
                "angular_y",
                "angular_z",
            ],
        )
        writer.writeheader()
        for bag_ts, msg in iter_topic_messages(reader, topic):
            pose = msg.pose.pose
            twist = msg.twist.twist
            header_ts = time_to_ns(msg.header.stamp)
            writer.writerow(
                {
                    "bag_timestamp_ns": bag_ts,
                    "bag_timestamp": ns_to_iso8601(bag_ts),
                    "header_timestamp_ns": header_ts,
                    "header_timestamp": ns_to_iso8601(header_ts),
                    "frame_id": msg.header.frame_id,
                    "child_frame_id": msg.child_frame_id,
                    "x": pose.position.x,
                    "y": pose.position.y,
                    "z": pose.position.z,
                    "qx": pose.orientation.x,
                    "qy": pose.orientation.y,
                    "qz": pose.orientation.z,
                    "qw": pose.orientation.w,
                    "yaw_rad": yaw_from_quaternion(
                        pose.orientation.x,
                        pose.orientation.y,
                        pose.orientation.z,
                        pose.orientation.w,
                    ),
                    "linear_x": twist.linear.x,
                    "linear_y": twist.linear.y,
                    "linear_z": twist.linear.z,
                    "angular_x": twist.angular.x,
                    "angular_y": twist.angular.y,
                    "angular_z": twist.angular.z,
                }
            )
            rows += 1
    return {"topic": topic, "rows": rows, "file": str(output_path)}


def export_pose_stamped(reader: Any, topic: str, output_path: Path) -> dict[str, Any]:
    rows = 0
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "bag_timestamp_ns",
                "bag_timestamp",
                "header_timestamp_ns",
                "header_timestamp",
                "frame_id",
                "x",
                "y",
                "z",
                "qx",
                "qy",
                "qz",
                "qw",
                "yaw_rad",
            ],
        )
        writer.writeheader()
        for bag_ts, msg in iter_topic_messages(reader, topic):
            pose = msg.pose
            header_ts = time_to_ns(msg.header.stamp)
            writer.writerow(
                {
                    "bag_timestamp_ns": bag_ts,
                    "bag_timestamp": ns_to_iso8601(bag_ts),
                    "header_timestamp_ns": header_ts,
                    "header_timestamp": ns_to_iso8601(header_ts),
                    "frame_id": msg.header.frame_id,
                    "x": pose.position.x,
                    "y": pose.position.y,
                    "z": pose.position.z,
                    "qx": pose.orientation.x,
                    "qy": pose.orientation.y,
                    "qz": pose.orientation.z,
                    "qw": pose.orientation.w,
                    "yaw_rad": yaw_from_quaternion(
                        pose.orientation.x,
                        pose.orientation.y,
                        pose.orientation.z,
                        pose.orientation.w,
                    ),
                }
            )
            rows += 1
    return {"topic": topic, "rows": rows, "file": str(output_path)}


def export_tf(reader: Any, topic: str, output_path: Path) -> dict[str, Any]:
    rows = 0
    pairs: set[tuple[str, str]] = set()
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "bag_timestamp_ns",
                "bag_timestamp",
                "header_timestamp_ns",
                "header_timestamp",
                "parent_frame",
                "child_frame",
                "tx",
                "ty",
                "tz",
                "qx",
                "qy",
                "qz",
                "qw",
                "yaw_rad",
            ],
        )
        writer.writeheader()
        for bag_ts, msg in iter_topic_messages(reader, topic):
            for tf in msg.transforms:
                header_ts = time_to_ns(tf.header.stamp)
                pairs.add((tf.header.frame_id, tf.child_frame_id))
                writer.writerow(
                    {
                        "bag_timestamp_ns": bag_ts,
                        "bag_timestamp": ns_to_iso8601(bag_ts),
                        "header_timestamp_ns": header_ts,
                        "header_timestamp": ns_to_iso8601(header_ts),
                        "parent_frame": tf.header.frame_id,
                        "child_frame": tf.child_frame_id,
                        "tx": tf.transform.translation.x,
                        "ty": tf.transform.translation.y,
                        "tz": tf.transform.translation.z,
                        "qx": tf.transform.rotation.x,
                        "qy": tf.transform.rotation.y,
                        "qz": tf.transform.rotation.z,
                        "qw": tf.transform.rotation.w,
                        "yaw_rad": yaw_from_quaternion(
                            tf.transform.rotation.x,
                            tf.transform.rotation.y,
                            tf.transform.rotation.z,
                            tf.transform.rotation.w,
                        ),
                    }
                )
                rows += 1
    return {"topic": topic, "rows": rows, "pairs": sorted(pairs), "file": str(output_path)}


def export_camera_info(reader: Any, topic: str, output_path: Path) -> dict[str, Any]:
    messages = list(iter_topic_messages(reader, topic))
    if not messages:
        return {"topic": topic, "rows": 0, "file": str(output_path)}

    bag_ts, msg = messages[0]
    payload = {
        "bag_timestamp_ns": bag_ts,
        "bag_timestamp": ns_to_iso8601(bag_ts),
        "frame_id": msg.header.frame_id,
        "width": msg.width,
        "height": msg.height,
        "distortion_model": msg.distortion_model,
        "d": list(msg.d),
        "k": list(msg.k),
        "r": list(msg.r),
        "p": list(msg.p),
        "binning_x": msg.binning_x,
        "binning_y": msg.binning_y,
        "roi": {
            "x_offset": msg.roi.x_offset,
            "y_offset": msg.roi.y_offset,
            "height": msg.roi.height,
            "width": msg.roi.width,
            "do_rectify": msg.roi.do_rectify,
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"topic": topic, "rows": 1, "file": str(output_path)}


def export_meta_strings(reader: Any, outputs: dict[str, Path]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for topic, path in outputs.items():
        messages = list(iter_topic_messages(reader, topic))
        if messages:
            _, msg = messages[0]
            path.write_text(msg.data, encoding="utf-8")
            result[topic] = {"value": msg.data, "file": str(path)}
        else:
            result[topic] = {"value": None, "file": str(path)}
    return result


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    AnyReader, typestore = load_reader_and_typestore()

    summary: dict[str, Any] = {"bag_dir": str(BAG_DIR), "exports": []}

    with AnyReader([BAG_DIR], default_typestore=typestore) as reader:
        for spec in ODOM_TOPICS:
            summary["exports"].append(export_odom(reader, spec.topic, OUTPUT_DIR / spec.output_name))

        for spec in POSE_TOPICS:
            summary["exports"].append(export_pose_stamped(reader, spec.topic, OUTPUT_DIR / spec.output_name))

        for spec in TF_TOPICS:
            summary["exports"].append(export_tf(reader, spec.topic, OUTPUT_DIR / spec.output_name))

        summary["exports"].append(export_camera_info(reader, CAMERA_INFO_TOPIC, OUTPUT_DIR / "camera_info_security_check.json"))
        summary["meta"] = export_meta_strings(reader, {topic: OUTPUT_DIR / name for topic, name in META_TOPICS.items()})

    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: exported pose/calibration data to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
