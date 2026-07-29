from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

_MODULE_PATH = Path(__file__).with_name("generate_rtsp_stream.py")
_SPEC = importlib.util.spec_from_file_location("generate_rtsp_stream", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
generate_rtsp_stream = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(generate_rtsp_stream)

build_ffmpeg_command = generate_rtsp_stream.build_ffmpeg_command
build_rtsp_url = generate_rtsp_stream.build_rtsp_url
timeline_unix_seconds = generate_rtsp_stream.timeline_unix_seconds


class GenerateRtspStreamTests(unittest.TestCase):
    def test_build_rtsp_url_strips_slashes(self) -> None:
        self.assertEqual(build_rtsp_url("127.0.0.1", 18554, "/live/"), "rtsp://127.0.0.1:18554/live")

    def test_default_timeline_start_matches_scene(self) -> None:
        self.assertEqual(int(timeline_unix_seconds(generate_rtsp_stream.DEFAULT_TIME_START) * 1000), 1774328748518)

    def test_video_mode_publishes_looping_start_anchored_time_channel(self) -> None:
        video = Path(__file__).resolve().parent / "rtsp_test" / "inspection.mp4"
        with (
            patch.object(generate_rtsp_stream, "resolve_drawtext_font", return_value=None),
            patch.object(generate_rtsp_stream, "probe_media_duration_seconds", return_value=417.2),
            patch.object(generate_rtsp_stream.os, "name", "posix"),
        ):
            command = build_ffmpeg_command(
                ffmpeg_bin="ffmpeg",
                mode="video",
                rtsp_url="rtsp://127.0.0.1:18554/live",
                width=1280,
                height=720,
                fps=25,
                video_path=video,
                time_rtsp_url="rtsp://127.0.0.1:18554/time",
            )

        joined = " ".join(command)
        self.assertIn("split=2", joined)
        self.assertIn("TIME SYNC", joined)
        self.assertIn("format=rgb24", joined)
        self.assertIn("mod(PTS*TB", joined)
        self.assertIn("1774328748.518000", joined)
        self.assertIn("%{pts\\:gmtime}", joined)
        self.assertIn("setpts=N/25/TB", joined)
        self.assertNotIn("%H\\:%M\\:%S", joined)
        self.assertNotIn("PTS*TB*1000", joined)
        self.assertNotIn("379.868000", joined)
        self.assertIn("-stream_loop", command)
        self.assertEqual(command.count("-f"), 2)
        self.assertIn("rtsp://127.0.0.1:18554/live", command)
        self.assertIn("rtsp://127.0.0.1:18554/time", command)

    def test_video_mode_can_disable_time_channel(self) -> None:
        video = Path(__file__).resolve().parent / "rtsp_test" / "inspection.mp4"
        with (
            patch.object(generate_rtsp_stream, "resolve_drawtext_font", return_value=None),
            patch.object(generate_rtsp_stream, "probe_media_duration_seconds", return_value=417.2),
        ):
            # Non-Windows path allows missing font; force non-nt check by patching os.name if needed.
            with patch.object(generate_rtsp_stream.os, "name", "posix"):
                command = build_ffmpeg_command(
                    ffmpeg_bin="ffmpeg",
                    mode="video",
                    rtsp_url="rtsp://127.0.0.1:18554/live",
                    width=1280,
                    height=720,
                    fps=25,
                    video_path=video,
                    time_rtsp_url=None,
                )

        joined = " ".join(command)
        self.assertNotIn("split=2", joined)
        self.assertIn("mod(PTS*TB", joined)
        self.assertIn("setpts=N/25/TB", joined)
        self.assertEqual(command.count("rtsp://127.0.0.1:18554/live"), 1)
        self.assertNotIn("rtsp://127.0.0.1:18554/time", command)

    def test_windows_requires_fontfile(self) -> None:
        video = Path(__file__).resolve().parent / "rtsp_test" / "inspection.mp4"
        with (
            patch.object(generate_rtsp_stream, "resolve_drawtext_font", return_value=None),
            patch.object(generate_rtsp_stream, "probe_media_duration_seconds", return_value=417.2),
            patch.object(generate_rtsp_stream.os, "name", "nt"),
        ):
            with self.assertRaisesRegex(RuntimeError, "No TrueType font"):
                build_ffmpeg_command(
                    ffmpeg_bin="ffmpeg",
                    mode="video",
                    rtsp_url="rtsp://127.0.0.1:18554/live",
                    width=1280,
                    height=720,
                    fps=25,
                    video_path=video,
                    time_rtsp_url=None,
                )


if __name__ == "__main__":
    unittest.main()
