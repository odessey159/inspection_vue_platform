from __future__ import annotations

from pathlib import Path

import yaml


def is_rosbag_dir(path: Path) -> bool:
    return path.is_dir() and (path / "metadata.yaml").exists() and any(path.glob("*.db3"))


def parse_rosbag_summary(bag_dir: Path) -> dict[str, object]:
    metadata_path = bag_dir / "metadata.yaml"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing rosbag metadata: {metadata_path}")

    raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    info = raw.get("rosbag2_bagfile_information", {})
    start_ns = int(info.get("starting_time", {}).get("nanoseconds_since_epoch", 0) or 0)
    duration_ns = int(info.get("duration", {}).get("nanoseconds", 0) or 0)

    topics: list[dict[str, object]] = []
    for entry in info.get("topics_with_message_count", []):
        metadata = entry.get("topic_metadata", {}) or {}
        topics.append(
            {
                "name": str(metadata.get("name", "")),
                "type": str(metadata.get("type", "")),
                "message_count": int(entry.get("message_count", 0) or 0),
            }
        )

    topics.sort(key=lambda item: item["message_count"], reverse=True)

    video_topic = _infer_topic(topics, names=("cam", "camera", "image"), types=("Image",))
    point_topic = _infer_topic(topics, names=("lidar", "point", "cloud"), types=("Point", "Points"))
    pose_topic = _infer_topic(
        topics,
        names=("odom", "pose", "slam"),
        types=("Odometry", "PoseStamped"),
    )

    return {
        "bag_dir": str(bag_dir.resolve()),
        "storage_identifier": str(info.get("storage_identifier", "")),
        "message_count": int(info.get("message_count", 0) or 0),
        "duration_ms": int(round(duration_ns / 1_000_000)) if duration_ns else 0,
        "start_ts_ms": int(round(start_ns / 1_000_000)) if start_ns else None,
        "end_ts_ms": int(round((start_ns + duration_ns) / 1_000_000)) if start_ns else None,
        "topics": topics,
        "preferred_topics": {
            "video": video_topic,
            "pointcloud": point_topic,
            "pose": pose_topic,
        },
    }


def _infer_topic(
    topics: list[dict[str, object]],
    names: tuple[str, ...],
    types: tuple[str, ...],
) -> str | None:
    lowered_names = tuple(name.lower() for name in names)
    for topic in topics:
        topic_name = str(topic["name"]).lower()
        topic_type = str(topic["type"])
        if any(name in topic_name for name in lowered_names) and any(token in topic_type for token in types):
            return str(topic["name"])
    for topic in topics:
        topic_name = str(topic["name"]).lower()
        if any(name in topic_name for name in lowered_names):
            return str(topic["name"])
    return None
