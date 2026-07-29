from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services import rtsp_timeline  # noqa: E402
from app.services.rtsp_recorder import align_scene_timestamps_to_video  # noqa: E402
from app.services.rtsp_timeline import (  # noqa: E402
    TIMESTAMP_BARCODE_BIT_WIDTH,
    TIMESTAMP_BARCODE_BITS,
    TIMESTAMP_BARCODE_HEIGHT,
    decode_timestamp_ms_from_image,
    derive_time_rtsp_url,
    read_recording_timeline_meta,
    resolve_recording_video_start_ts,
    write_recording_timeline_meta,
)


def _paint_barcode(image: Image.Image, timestamp_ms: int) -> None:
    for bit in range(TIMESTAMP_BARCODE_BITS):
        on = bool((timestamp_ms >> bit) & 1)
        color = (255, 255, 255) if on else (0, 0, 0)
        x0 = bit * TIMESTAMP_BARCODE_BIT_WIDTH
        for y in range(TIMESTAMP_BARCODE_HEIGHT):
            for x in range(x0, x0 + TIMESTAMP_BARCODE_BIT_WIDTH):
                image.putpixel((x, y), color)


class RtspTimelineTests(unittest.TestCase):
    def test_derive_time_rtsp_url_from_live(self) -> None:
        self.assertEqual(
            derive_time_rtsp_url("rtsp://127.0.0.1:18554/live"),
            "rtsp://127.0.0.1:18554/time",
        )

    def test_decode_timestamp_barcode_roundtrip(self) -> None:
        expected = 1_774_328_748_518
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "frame.png"
            image = Image.new(
                "RGB",
                (TIMESTAMP_BARCODE_BITS * TIMESTAMP_BARCODE_BIT_WIDTH + 8, TIMESTAMP_BARCODE_HEIGHT + 8),
                color=(20, 20, 20),
            )
            _paint_barcode(image, expected)
            image.save(path)
            self.assertEqual(decode_timestamp_ms_from_image(path), expected)

    def test_recording_meta_preferred_over_filename_wall_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            recording = Path(tmp_dir) / "recording_20260720T120000Z.mp4"
            recording.write_bytes(b"fake")
            write_recording_timeline_meta(
                recording,
                video_start_ts=1_774_328_748_518,
                source="rtsp_time_barcode",
                rtsp_url="rtsp://127.0.0.1:18554/live",
            )
            meta = read_recording_timeline_meta(recording)
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.timestamp_ms, 1_774_328_748_518)

            from app.services.rtsp_recorder import _recording_start_ts

            self.assertEqual(_recording_start_ts(recording), 1_774_328_748_518)

    def test_resolve_recording_video_start_ts_prefers_rtsp_sample(self) -> None:
        sample = rtsp_timeline.RtspTimelineSample(
            timestamp_ms=1_774_328_748_518,
            source="rtsp_time_barcode",
        )
        with patch.object(rtsp_timeline, "sample_timestamp_ms_from_rtsp", return_value=sample.timestamp_ms):
            resolved = resolve_recording_video_start_ts("rtsp://127.0.0.1:18554/live")
        self.assertEqual(resolved.timestamp_ms, sample.timestamp_ms)
        self.assertEqual(resolved.source, "rtsp_time_barcode")

    def test_resolve_falls_back_to_map_origin(self) -> None:
        with patch.object(rtsp_timeline, "sample_timestamp_ms_from_rtsp", return_value=None):
            with patch.object(rtsp_timeline, "map_timeline_origin_ms", return_value=1_774_328_748_518):
                resolved = resolve_recording_video_start_ts("rtsp://127.0.0.1:18554/live")
        self.assertEqual(resolved.timestamp_ms, 1_774_328_748_518)
        self.assertEqual(resolved.source, "map_timeline_origin")

    def test_align_keeps_shared_rtsp_timeline(self) -> None:
        scene = {
            "trajectory_timestamps": [1_774_328_748_518, 1_774_329_000_000],
            "notes": [],
            "scene_quality": {},
        }
        aligned = align_scene_timestamps_to_video(
            scene,
            1_774_328_800_000,
            1_774_328_860_000,
            map_source="/maps/scene.json",
        )
        self.assertEqual(
            aligned["trajectory_timestamps"],
            [1_774_328_748_518, 1_774_329_000_000],
        )
        self.assertEqual(aligned["scene_quality"]["time_alignment"]["mode"], "rtsp_timeline_shared")


if __name__ == "__main__":
    unittest.main()
