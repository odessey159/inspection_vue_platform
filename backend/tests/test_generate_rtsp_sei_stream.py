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
parse_args = sei_stream.parse_args
source_frame_timestamp_ms = sei_stream.source_frame_timestamp_ms
yaw_from_quaternion = sei_stream.yaw_from_quaternion


_VIDEO_TS0_S = 1_774_328_711.447
_SCENE_TS0_MS = 1_774_328_748_518


class GenerateRtspSeiStreamTests(unittest.TestCase):
    def test_default_mode_uses_quad_video_protocol(self) -> None:
        self.assertEqual(parse_args([]).mode, "quad-video")

    def test_default_time_start_matches_inspection_video_epoch(self) -> None:
        self.assertEqual(parse_args([]).time_start, "2026-03-24 05:05:11.447")

    def test_frame_zero_matches_inspection_video_epoch(self) -> None:
        timestamp_ns = frame_timestamp_ns(0, start_s=_VIDEO_TS0_S, fps=25, duration_s=417.2)
        self.assertEqual(timestamp_ns // 1_000_000, 1_774_328_711_447)

    def test_sei_timestamp_is_monotonic_but_pose_clock_wraps(self) -> None:
        first_loop = frame_timestamp_ns(0, start_s=_VIDEO_TS0_S, fps=25, duration_s=2.0)
        second_loop = frame_timestamp_ns(50, start_s=_VIDEO_TS0_S, fps=25, duration_s=2.0)
        wrapped_pose_time = source_frame_timestamp_ms(
            50,
            start_s=_VIDEO_TS0_S,
            fps=25,
            duration_s=2.0,
        )
        self.assertEqual(second_loop - first_loop, 2_000_000_000)
        self.assertEqual(wrapped_pose_time, first_loop // 1_000_000)

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
        self.assertNotIn("mod(PTS*TB", joined)
        self.assertIn("1774328711.447000", joined)
        self.assertIn("setpts=N/25/TB", joined)

    def test_quad_video_encode_builds_labeled_mosaic(self) -> None:
        video = Path(__file__).resolve().parent / "rtsp_test" / "inspection.mp4"
        with (
            patch.object(sei_stream.barcode_stream, "resolve_drawtext_font", return_value=None),
            patch.object(sei_stream.barcode_stream, "probe_media_duration_seconds", return_value=417.2),
            patch.object(sei_stream.os, "name", "posix"),
            patch.object(sei_stream.barcode_stream.os, "name", "posix"),
        ):
            command = build_encode_command(
                ffmpeg_bin="ffmpeg",
                mode="quad-video",
                width=1280,
                height=720,
                fps=25,
                video_path=video,
            )

        joined = " ".join(command)
        self.assertIn("split=4", joined)
        self.assertIn("FRONT", joined)
        self.assertIn("REAR", joined)
        self.assertIn("LEFT", joined)
        self.assertIn("RIGHT", joined)
        self.assertIn("hstack=inputs=2", joined)
        self.assertIn("vstack=inputs=2", joined)
        self.assertIn("-stream_loop", command)
        self.assertIn("setpts=N/25/TB", joined)
        self.assertEqual(command[command.index("-map") + 1], "[vout]")

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
            start_s=_VIDEO_TS0_S,
            fps=25,
            duration_s=None,
            poses=None,
        )
        self.assertEqual(metadata.x, 0.0)
        self.assertEqual(metadata.y, 0.0)
        self.assertEqual(metadata.yaw, 0.0)
        self.assertEqual(metadata.timestamp_ms, 1_774_328_711_447)

    def test_scene_pose_starts_at_matching_video_offset(self) -> None:
        frame_index = round((_SCENE_TS0_MS / 1000.0 - _VIDEO_TS0_S) * 25)
        pose_timestamp_ms = source_frame_timestamp_ms(
            frame_index,
            start_s=_VIDEO_TS0_S,
            fps=25,
            duration_s=417.2,
        )
        self.assertLessEqual(abs(pose_timestamp_ms - _SCENE_TS0_MS), 20)


if __name__ == "__main__":
    unittest.main()
