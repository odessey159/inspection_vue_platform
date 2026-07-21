from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.rtsp_live import MJPEG_BOUNDARY, build_rtsp_mjpeg_ffmpeg_command, iter_mjpeg_multipart_frames


class RtspLiveServiceTest(unittest.TestCase):
    def test_build_rtsp_mjpeg_ffmpeg_command(self) -> None:
        command = build_rtsp_mjpeg_ffmpeg_command(
            rtsp_url="rtsp://127.0.0.1:18554/live",
            rtsp_transport="tcp",
        )
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-rtsp_transport", command)
        self.assertIn("tcp", command)
        self.assertIn("rtsp://127.0.0.1:18554/live", command)
        self.assertIn("mjpeg", command)
        self.assertIn("fps=15", command)

    def test_iter_mjpeg_multipart_frames_splits_jpeg_payload(self) -> None:
        jpeg = b"\xff\xd8\x01\x02\xff\xd9"
        fake_process = type(
            "FakeProcess",
            (),
            {
                "stdout": type("FakeStdout", (), {"read": lambda self, size: jpeg if not getattr(self, "_done", False) else b""})(),
                "poll": lambda self: 0,
                "kill": lambda self: None,
                "wait": lambda self, timeout=None: 0,
            },
        )()
        fake_process.stdout._done = False

        def fake_read(size: int) -> bytes:
            if fake_process.stdout._done:
                return b""
            fake_process.stdout._done = True
            return jpeg

        fake_process.stdout.read = fake_read

        with patch("app.services.rtsp_live.resolve_recording_rtsp_url", return_value="rtsp://127.0.0.1:18554/live"), patch(
            "app.services.rtsp_live.subprocess.Popen",
            return_value=fake_process,
        ):
            frames = list(iter_mjpeg_multipart_frames("rtsp://127.0.0.1:18554/live"))

        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].startswith(f"--{MJPEG_BOUNDARY}".encode("ascii")))
        self.assertIn(jpeg, frames[0])


if __name__ == "__main__":
    unittest.main()
