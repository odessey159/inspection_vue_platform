#!/usr/bin/env python3
"""Inspect and extract ROS 2 rosbag2 sqlite bags.

This script has two usage levels:
1. `topics`: pure standard-library SQLite inspection, works without ROS.
2. `dump`: deserializes one topic and prints JSON lines.
   - Preferred backend: ROS 2 APIs in a sourced ROS environment.
   - Fallback backend: pure Python `rosbags`, useful on Windows without ROS.
"""

from __future__ import annotations

import argparse
import array
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MSG_DIR = REPO_ROOT / "msg_display" / "src" / "common" / "tidepilot_msgs" / "msg"
VENDOR_DIR_CANDIDATES = [REPO_ROOT / ".vendor_local", REPO_ROOT / ".vendor"]


@dataclass
class TopicStats:
    topic: str
    msg_type: str
    count: int = 0
    first_ns: int | None = None
    last_ns: int | None = None

    def update(self, count: int, first_ns: int | None, last_ns: int | None) -> None:
        self.count += count
        if first_ns is not None:
            self.first_ns = first_ns if self.first_ns is None else min(self.first_ns, first_ns)
        if last_ns is not None:
            self.last_ns = last_ns if self.last_ns is None else max(self.last_ns, last_ns)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or extract ROS 2 rosbag2 sqlite bags.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    topics_parser = subparsers.add_parser(
        "topics",
        help="List topics, message counts, and time ranges using only sqlite3.",
    )
    topics_parser.add_argument("bag_dir", type=Path, help="Path to the rosbag2 directory.")

    dump_parser = subparsers.add_parser(
        "dump",
        help="Deserialize one topic and print JSON lines.",
    )
    dump_parser.add_argument("bag_dir", type=Path, help="Path to the rosbag2 directory.")
    dump_parser.add_argument("--topic", required=True, help="Topic name, for example /vehicle_info")
    dump_parser.add_argument("--limit", type=int, default=5, help="How many messages to output.")
    dump_parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Skip this many matched messages before printing.",
    )
    dump_parser.add_argument(
        "--max-seq-preview",
        type=int,
        default=16,
        help="Preview length for large arrays or byte sequences in JSON output.",
    )
    dump_parser.add_argument(
        "--output",
        type=Path,
        help="Optional output file. Defaults to stdout.",
    )
    dump_parser.add_argument(
        "--storage-id",
        default="sqlite3",
        help="rosbag2 storage id. Defaults to sqlite3.",
    )
    dump_parser.add_argument(
        "--backend",
        choices=("auto", "ros2", "rosbags"),
        default="auto",
        help="Deserializer backend. Defaults to auto.",
    )
    dump_parser.add_argument(
        "--msg-dir",
        type=Path,
        default=DEFAULT_MSG_DIR,
        help="Directory containing custom .msg definitions for rosbags fallback.",
    )

    return parser.parse_args()


def ensure_bag_dir(bag_dir: Path) -> Path:
    bag_dir = bag_dir.resolve()
    if not bag_dir.exists():
        raise SystemExit(f"Bag directory does not exist: {bag_dir}")
    db_files = sorted(bag_dir.glob("*.db3"))
    if not db_files:
        raise SystemExit(f"No .db3 files found under: {bag_dir}")
    return bag_dir


def ns_to_iso8601(timestamp_ns: int | None) -> str:
    if timestamp_ns is None:
        return "-"
    dt = datetime.fromtimestamp(timestamp_ns / 1e9, tz=timezone.utc).astimezone()
    return dt.isoformat()


def iter_db_files(bag_dir: Path) -> Iterable[Path]:
    return sorted(bag_dir.glob("*.db3"))


def collect_topic_stats(bag_dir: Path) -> tuple[list[TopicStats], int | None, int | None, int]:
    aggregated: dict[tuple[str, str], TopicStats] = {}
    bag_first_ns: int | None = None
    bag_last_ns: int | None = None
    total_messages = 0

    for db_file in iter_db_files(bag_dir):
        conn = sqlite3.connect(str(db_file))
        try:
            rows = conn.execute(
                """
                SELECT
                    t.name,
                    t.type,
                    COUNT(m.id) AS msg_count,
                    MIN(m.timestamp) AS first_ts,
                    MAX(m.timestamp) AS last_ts
                FROM topics t
                LEFT JOIN messages m ON m.topic_id = t.id
                GROUP BY t.id, t.name, t.type
                ORDER BY t.name
                """
            ).fetchall()
        finally:
            conn.close()

        for name, msg_type, msg_count, first_ts, last_ts in rows:
            key = (name, msg_type)
            if key not in aggregated:
                aggregated[key] = TopicStats(topic=name, msg_type=msg_type)
            aggregated[key].update(int(msg_count), first_ts, last_ts)
            total_messages += int(msg_count)

            if first_ts is not None:
                bag_first_ns = first_ts if bag_first_ns is None else min(bag_first_ns, first_ts)
            if last_ts is not None:
                bag_last_ns = last_ts if bag_last_ns is None else max(bag_last_ns, last_ts)

    return (
        sorted(aggregated.values(), key=lambda item: (-item.count, item.topic)),
        bag_first_ns,
        bag_last_ns,
        total_messages,
    )


def cmd_topics(args: argparse.Namespace) -> int:
    bag_dir = ensure_bag_dir(args.bag_dir)
    stats, bag_first_ns, bag_last_ns, total_messages = collect_topic_stats(bag_dir)

    print(f"bag_dir: {bag_dir}")
    print(f"db_files: {len(list(iter_db_files(bag_dir)))}")
    print(f"total_messages: {total_messages}")
    print(f"start_time: {ns_to_iso8601(bag_first_ns)}")
    print(f"end_time: {ns_to_iso8601(bag_last_ns)}")
    if bag_first_ns is not None and bag_last_ns is not None:
        duration_seconds = (bag_last_ns - bag_first_ns) / 1e9
        print(f"duration_seconds: {duration_seconds:.3f}")
    print()
    print("count\tfirst_time\tlast_time\ttopic\ttype")
    for item in stats:
        print(
            f"{item.count}\t{ns_to_iso8601(item.first_ns)}\t{ns_to_iso8601(item.last_ns)}\t"
            f"{item.topic}\t{item.msg_type}"
        )
    return 0


def try_import_ros_deps() -> tuple[Any, Any, Any, Any, Any, Any] | None:
    try:
        from rclpy.serialization import deserialize_message
        from rosbag2_py import ConverterOptions, SequentialReader, StorageFilter, StorageOptions
        from rosidl_runtime_py.utilities import get_message
    except ImportError:
        return None
    return deserialize_message, ConverterOptions, SequentialReader, StorageOptions, StorageFilter, get_message


def try_import_rosbags_deps() -> tuple[Any, Any, Any, Any] | None:
    candidates = [candidate for candidate in VENDOR_DIR_CANDIDATES if candidate.exists()]
    for candidate in reversed(candidates):
        if candidate.exists():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)

    try:
        from rosbags.highlevel import AnyReader
        from rosbags.typesys import Stores, get_typestore, get_types_from_msg
    except ImportError:
        return None

    return AnyReader, Stores, get_typestore, get_types_from_msg


def ros_message_to_builtin(value: Any, max_seq_preview: int) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, (bytes, bytearray)):
        preview = list(value[:max_seq_preview])
        payload = {"length": len(value), "preview": preview}
        if len(value) > max_seq_preview:
            payload["truncated"] = True
        return payload

    if isinstance(value, array.array):
        data = value.tolist()
        if len(data) > max_seq_preview:
            return {
                "length": len(data),
                "preview": data[:max_seq_preview],
                "truncated": True,
            }
        return data

    if hasattr(value, "tolist") and value.__class__.__module__.startswith("numpy"):
        data = value.tolist()
        if isinstance(data, list) and len(data) > max_seq_preview:
            return {
                "length": len(data),
                "preview": data[:max_seq_preview],
                "truncated": True,
            }
        return data

    if isinstance(value, (list, tuple)):
        if len(value) > max_seq_preview:
            return {
                "length": len(value),
                "preview": [ros_message_to_builtin(item, max_seq_preview) for item in value[:max_seq_preview]],
                "truncated": True,
            }
        return [ros_message_to_builtin(item, max_seq_preview) for item in value]

    if hasattr(value, "sec") and hasattr(value, "nanosec"):
        return {"sec": int(value.sec), "nanosec": int(value.nanosec)}

    if hasattr(value, "get_fields_and_field_types"):
        result = {}
        for field_name in value.get_fields_and_field_types().keys():
            result[field_name] = ros_message_to_builtin(getattr(value, field_name), max_seq_preview)
        return result

    if hasattr(value, "__dict__"):
        result = {}
        for field_name, field_value in vars(value).items():
            if field_name.startswith("_") or field_name == "__msgtype__":
                continue
            result[field_name] = ros_message_to_builtin(field_value, max_seq_preview)
        return result

    return str(value)


def find_topic_type(reader: Any, topic_name: str) -> str:
    for topic_info in reader.get_all_topics_and_types():
        if topic_info.name == topic_name:
            return topic_info.type
    available = ", ".join(sorted(info.name for info in reader.get_all_topics_and_types()))
    raise SystemExit(f"Topic not found: {topic_name}\nAvailable topics: {available}")


def open_output(path: Path | None):
    if path is None:
        return sys.stdout, False
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8"), True


def load_custom_types(msg_dir: Path, get_types_from_msg: Any, typestore: Any) -> int:
    msg_dir = msg_dir.resolve()
    if not msg_dir.exists():
        return 0

    custom_types: dict[str, Any] = {}
    for path in sorted(msg_dir.glob("*.msg")):
        type_name = f"tidepilot_msgs/msg/{path.stem}"
        custom_types.update(get_types_from_msg(path.read_text(encoding="utf-8"), type_name))

    if custom_types:
        typestore.register(custom_types)

    return len(custom_types)


def dump_with_ros2(args: argparse.Namespace, ros_deps: tuple[Any, Any, Any, Any, Any, Any]) -> int:
    bag_dir = ensure_bag_dir(args.bag_dir)
    (
        deserialize_message,
        ConverterOptions,
        SequentialReader,
        StorageOptions,
        StorageFilter,
        get_message,
    ) = import_ros_deps()

    reader = SequentialReader()
    reader.open(
        StorageOptions(uri=str(bag_dir), storage_id=args.storage_id),
        ConverterOptions(input_serialization_format="", output_serialization_format=""),
    )

    try:
        reader.set_filter(StorageFilter(topics=[args.topic]))
    except Exception:
        pass

    msg_type_name = find_topic_type(reader, args.topic)
    msg_cls = get_message(msg_type_name)

    output_stream, should_close = open_output(args.output)
    written = 0
    seen = 0
    try:
        while reader.has_next():
            topic, raw_data, timestamp_ns = reader.read_next()
            if topic != args.topic:
                continue
            if seen < args.start_index:
                seen += 1
                continue

            msg = deserialize_message(raw_data, msg_cls)
            record = {
                "index": seen,
                "topic": topic,
                "type": msg_type_name,
                "timestamp_ns": int(timestamp_ns),
                "timestamp": ns_to_iso8601(int(timestamp_ns)),
                "message": ros_message_to_builtin(msg, args.max_seq_preview),
            }
            output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            seen += 1
            if written >= args.limit:
                break
    finally:
        if should_close:
            output_stream.close()

    if written == 0:
        raise SystemExit(
            f"No messages written for topic {args.topic}. "
            f"start-index={args.start_index}, limit={args.limit}"
        )

    return 0


def dump_with_rosbags(args: argparse.Namespace, rosbags_deps: tuple[Any, Any, Any, Any]) -> int:
    bag_dir = ensure_bag_dir(args.bag_dir)
    AnyReader, Stores, get_typestore, get_types_from_msg = rosbags_deps

    typestore = get_typestore(Stores.ROS2_HUMBLE)
    load_custom_types(args.msg_dir, get_types_from_msg, typestore)

    output_stream, should_close = open_output(args.output)
    written = 0
    seen = 0
    try:
        with AnyReader([bag_dir], default_typestore=typestore) as reader:
            connections = [conn for conn in reader.connections if conn.topic == args.topic]
            if not connections:
                available = ", ".join(sorted(reader.topics.keys()))
                raise SystemExit(f"Topic not found: {args.topic}\nAvailable topics: {available}")

            msg_type_name = connections[0].msgtype
            for conn, timestamp_ns, raw_data in reader.messages(connections=connections):
                if seen < args.start_index:
                    seen += 1
                    continue

                msg = reader.deserialize(raw_data, conn.msgtype)
                record = {
                    "index": seen,
                    "topic": conn.topic,
                    "type": msg_type_name,
                    "timestamp_ns": int(timestamp_ns),
                    "timestamp": ns_to_iso8601(int(timestamp_ns)),
                    "message": ros_message_to_builtin(msg, args.max_seq_preview),
                }
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
                seen += 1
                if written >= args.limit:
                    break
    finally:
        if should_close:
            output_stream.close()

    if written == 0:
        raise SystemExit(
            f"No messages written for topic {args.topic}. "
            f"start-index={args.start_index}, limit={args.limit}"
        )

    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    if args.backend in ("auto", "ros2"):
        ros_deps = try_import_ros_deps()
        if ros_deps is not None:
            return dump_with_ros2(args, ros_deps)
        if args.backend == "ros2":
            raise SystemExit(
                "ROS 2 Python dependencies are unavailable. Run this inside the configured ROS 2 "
                "environment after building and sourcing the workspace. Example:\n"
                "  source /opt/ros/humble/setup.bash\n"
                "  cd /root/tidepilot/msg_display && colcon build\n"
                "  source install/setup.bash"
            )

    if args.backend in ("auto", "rosbags"):
        rosbags_deps = try_import_rosbags_deps()
        if rosbags_deps is not None:
            return dump_with_rosbags(args, rosbags_deps)
        if args.backend == "rosbags":
            raise SystemExit(
                "Pure Python rosbags backend is unavailable. Install it into the workspace, "
                "for example under .vendor_local, and retry."
            )

    raise SystemExit(
        "No dump backend is available. ROS 2 is not importable, and rosbags is also missing."
    )


def main() -> int:
    args = parse_args()
    if args.command == "topics":
        return cmd_topics(args)
    if args.command == "dump":
        return cmd_dump(args)
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
