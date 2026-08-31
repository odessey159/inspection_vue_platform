from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_MODULE_PATH = Path(__file__).with_name("generate_rtsp_sei_stream.py")
_SPEC = importlib.util.spec_from_file_location("generate_rtsp_sei_stream", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
sei_stream = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sei_stream
_SPEC.loader.exec_module(sei_stream)

build_encode_command = sei_stream.build_encode_command
build_publish_command = sei_stream.build_publish_command
frame_timestamp_ns = sei_stream.frame_timestamp_ns
load_scene_pose_table = sei_stream.load_scene_pose_table
metadata_for_frame = sei_stream.metadata_for_frame
yaw_from_quaternion = sei_stream.yaw_from_quaternion


_TS0_S = 1_774_328_748.518


class GenerateRtspSeiStreamTests(unittest.TestCase):
    def test_frame_zero_matches_scene_epoch(self) -> None:
        timestamp_ns = frame_timestamp_ns(0, start_s=_TS0_S, fps=25, duration_s=417.2)
        self.assertEqual(timestamp_ns // 1_000_000, 1_774_328_748_518)

    def test_timestamp_wraps_with_loop_duration(self) -> None:
        first_loop = frame_timestamp_ns(0, start_s=_TS0_S, fps=25, duration_s=2.0)
        wrapped = frame_timestamp_ns(50, start_s=_TS0_S, fps=25, duration_s=2.0)
        self.assertEqual(first_loop, wrapped)

    def test_video_encode_writes_annexb_not_rtsp(self) -> None:
        video = Path(__file__).resolve().parent / "rtsp_test" / "inspection.mp4"
        with (
            patch.object(sei_stream.barcode_stream, "resolve_drawtext_font", return_value=None),
            patch.object(sei_stream.barcode_stream, "probe_media_duration_seconds", return_value=417.2),
            patch.object(sei_stream.os, "name", "posix"),
            patch.object(sei_stream.barcode_stream.os, "name", "posix"),
        ):
            command = build_encode_command(
                ffmpeg_bin="ffmpeg",
                mode="video",
                width=1280,
                height=720,
                fps=25,
                video_path=video,
            )
        joined = " ".join(command)
        self.assertIn("-f", command)
        self.assertIn("h264", command)
        self.assertIn("pipe:1", command)
        self.assertIn("aud=1:repeat-headers=1:bframes=0", command)
        self.assertNotIn("rtsp://", joined)
        self.assertNotIn("format=rgb24", joined)
        self.assertIn("setpts=N/25/TB", joined)

    def test_publish_command_copies_to_rtsp(self) -> None:
        command = build_publish_command(
            ffmpeg_bin="ffmpeg",
            fps=25,
            rtsp_url="rtsp://127.0.0.1:18554/live",
        )
        self.assertIn("pipe:0", command)
        self.assertIn("rtsp://127.0.0.1:18554/live", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")

    def test_pose_table_interpolates_and_unwraps_yaw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "scene.json"
            path.write_text(
                json.dumps(
                    {
                        "trajectory": [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
                        "trajectory_timestamps": [1_774_328_748_518, 1_774_328_749_518],
                        "trajectory_orientations": [
                            [0.0, 0.0, 0.0, 1.0],
                            [0.0, 0.0, 0.7071068, 0.7071068],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            table = load_scene_pose_table(path)
        x, y, yaw = table.at_ms(1_774_328_749_018)
        self.assertAlmostEqual(x, 5.0, places=5)
        self.assertAlmostEqual(y, 0.0, places=5)
        self.assertAlmostEqual(yaw, yaw_from_quaternion(0.0, 0.0, 0.7071068, 0.7071068) / 2.0, places=3)

    def test_metadata_without_scene_uses_zero_pose(self) -> None:
        metadata = metadata_for_frame(
            0,
            start_s=_TS0_S,
            fps=25,
            duration_s=None,
            poses=None,
        )
        self.assertEqual(metadata.x, 0.0)
        self.assertEqual(metadata.y, 0.0)
        self.assertEqual(metadata.yaw, 0.0)
        self.assertEqual(metadata.timestamp_ms, 1_774_328_748_518)


if __name__ == "__main__":
    unittest.main()
