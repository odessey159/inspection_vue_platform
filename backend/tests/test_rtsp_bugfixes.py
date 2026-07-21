from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services import rtsp_recorder


class ResolveStorageKeyTest(unittest.TestCase):
    def test_localhost_and_docker_internal_share_vehicle_id(self) -> None:
        vehicles = [
            type("Vehicle", (), {"id": "local-demo", "rtsp_url": "rtsp://127.0.0.1:18554/live"})(),
        ]
        with patch("app.services.rtsp_vehicles.load_rtsp_vehicles", return_value=vehicles):
            self.assertEqual(
                rtsp_recorder.resolve_storage_key_for_rtsp_url("rtsp://127.0.0.1:18554/live"),
                "local-demo",
            )
            self.assertEqual(
                rtsp_recorder.resolve_storage_key_for_rtsp_url("rtsp://host.docker.internal:18554/live"),
                "local-demo",
            )

    def test_fallback_storage_key_canonicalizes_localhost_aliases(self) -> None:
        with patch("app.services.rtsp_vehicles.load_rtsp_vehicles", return_value=[]):
            self.assertEqual(
                rtsp_recorder.resolve_storage_key_for_rtsp_url("rtsp://localhost:18554/live"),
                "127.0.0.1_live",
            )
            self.assertEqual(
                rtsp_recorder.resolve_storage_key_for_rtsp_url("rtsp://host.docker.internal:18554/live"),
                "127.0.0.1_live",
            )


class CleanupRtspRecordingsTest(unittest.TestCase):
    def test_cleanup_skips_monitor_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording = root / "local-demo" / "recording_old.mp4"
            capture = root / "local-demo" / "monitor_captures" / "segment_000000.mp4"
            llm_clip = root / "local-demo" / "monitor_llm_clips" / "clip.mp4"
            recording.parent.mkdir(parents=True)
            capture.parent.mkdir(parents=True)
            llm_clip.parent.mkdir(parents=True)
            for path in (recording, capture, llm_clip):
                path.write_bytes(b"x" * 16)
                path.touch()

            import os
            import time

            old = time.time() - 10_000
            os.utime(recording, (old, old))
            os.utime(capture, (old, old))
            os.utime(llm_clip, (old, old))

            with patch.object(rtsp_recorder, "RTSP_RECORDINGS_DIR", root):
                result = rtsp_recorder.cleanup_rtsp_recordings(max_age_seconds=60)

            self.assertEqual(result["deleted_files"], 1)
            self.assertFalse(recording.exists())
            self.assertTrue(capture.exists())
            self.assertTrue(llm_clip.exists())


class ClipRelativeTimeTest(unittest.TestCase):
    def test_absolute_detection_inside_clip_window(self) -> None:
        from app.services.provider import PreparedClip
        from app.services.provider_YOLO import _clip_relative_time_sec

        clip = PreparedClip(
            index=0,
            path=Path("."),
            start_offset_sec=25.0,
            duration_sec=25.0,
            start_ts_ms=0,
            byte_size=1,
            profile_name="t",
        )
        self.assertEqual(_clip_relative_time_sec(clip, 26.0), 1.0)
        # Absolute times before the clip window clamp to the clip start.
        self.assertEqual(_clip_relative_time_sec(clip, 5.0), 0.0)
        self.assertEqual(_clip_relative_time_sec(clip, 60.0), 25.0)


if __name__ == "__main__":
    unittest.main()
