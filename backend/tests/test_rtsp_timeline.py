from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services import rtsp_timeline  # noqa: E402
from app.services.rtsp_recorder import align_scene_timestamps_to_video  # noqa: E402
from app.services.rtsp_sei import FrameMetadata, PictureMetadata  # noqa: E402
from app.services.rtsp_timeline import (  # noqa: E402
    TIMESTAMP_BARCODE_BIT_WIDTH,
    TIMESTAMP_BARCODE_BITS,
    TIMESTAMP_BARCODE_HEIGHT,
    copy_recording_timeline_sidecars,
    decode_timestamp_ms_from_image,
    derive_time_rtsp_url,
    lookup_frame_metadata_for_timestamp,
    persist_recording_frame_metadata,
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
                source="rtsp_sei",
                rtsp_url="rtsp://127.0.0.1:18554/live",
                x=1.25,
                y=-3.5,
                yaw=0.2,
            )
            meta = read_recording_timeline_meta(recording)
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.timestamp_ms, 1_774_328_748_518)
            self.assertAlmostEqual(meta.x or 0.0, 1.25, places=5)
            self.assertAlmostEqual(meta.y or 0.0, -3.5, places=5)
            self.assertAlmostEqual(meta.yaw or 0.0, 0.2, places=5)

            from app.services.rtsp_recorder import _recording_start_ts

            self.assertEqual(_recording_start_ts(recording), 1_774_328_748_518)

    def test_persist_recording_writes_one_jsonl_row_per_picture(self) -> None:
        pictures = (
            PictureMetadata(
                frame_index=0,
                metadata=FrameMetadata(timestamp_ns=1_774_328_748_518_000_000, x=1.0, y=2.0, yaw=0.3),
            ),
            PictureMetadata(frame_index=1, metadata=None),
            PictureMetadata(
                frame_index=2,
                metadata=FrameMetadata(timestamp_ns=1_774_328_748_598_000_000, x=1.5, y=2.5, yaw=0.4),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            recording = Path(tmp_dir) / "recording_20260720T120000Z.mp4"
            recording.write_bytes(b"\x00" * 64)
            write_recording_timeline_meta(
                recording,
                video_start_ts=1,
                source="wall_clock",
                rtsp_url="rtsp://127.0.0.1:18554/live",
            )
            with patch.object(rtsp_timeline, "extract_picture_metadata_from_video", return_value=pictures):
                stored = persist_recording_frame_metadata(recording)
            self.assertEqual(len(stored), 3)
            sidecar = recording.with_name("recording_20260720T120000Z.frames.jsonl")
            self.assertTrue(sidecar.is_file())
            lines = [line for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 3)
            meta = read_recording_timeline_meta(recording)
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.timestamp_ms, 1_774_328_748_518)
            self.assertEqual(meta.source, "rtsp_sei")
            payload = json.loads(recording.with_name("recording_20260720T120000Z.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["frame_count"], 3)
            self.assertEqual(payload["sei_frame_count"], 2)

            copied = Path(tmp_dir) / "inspection.mp4"
            copied.write_bytes(b"\x00" * 64)
            copy_recording_timeline_sidecars(recording, copied)
            self.assertTrue(copied.with_name("inspection.frames.jsonl").is_file())
            nearest = lookup_frame_metadata_for_timestamp(stored, 1_774_328_748_590)
            self.assertIsNotNone(nearest)
            assert nearest is not None and nearest.metadata is not None
            self.assertEqual(nearest.frame_index, 2)

    def test_copy_sidecars_removes_stale_destination_file_when_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            recording = root / "recording.mp4"
            copied = root / "inspection.mp4"
            recording.write_bytes(b"new-video")
            copied.write_bytes(b"old-video")
            write_recording_timeline_meta(
                recording,
                video_start_ts=1_700_000_000_000,
                source="rtsp_sei",
            )
            stale_frames = copied.with_name("inspection.frames.jsonl")
            stale_frames.write_text('{"frame_index": 0}\n', encoding="utf-8")

            copy_recording_timeline_sidecars(recording, copied)

            self.assertTrue(copied.with_name("inspection.meta.json").is_file())
            self.assertFalse(stale_frames.exists())

    def test_resolve_recording_video_start_ts_prefers_sei(self) -> None:
        sei = FrameMetadata(
            timestamp_ns=1_774_328_748_518_000_000,
            x=1.0,
            y=2.0,
            yaw=0.3,
        )
        with patch.object(rtsp_timeline, "sample_sei_from_rtsp", return_value=sei):
            with patch.object(rtsp_timeline, "sample_timestamp_ms_from_rtsp") as barcode:
                resolved = resolve_recording_video_start_ts("rtsp://127.0.0.1:18554/live")
        barcode.assert_not_called()
        self.assertEqual(resolved.timestamp_ms, 1_774_328_748_518)
        self.assertEqual(resolved.source, "rtsp_sei")
        self.assertAlmostEqual(resolved.x or 0.0, 1.0, places=5)
        self.assertAlmostEqual(resolved.y or 0.0, 2.0, places=5)
        self.assertAlmostEqual(resolved.yaw or 0.0, 0.3, places=5)

    def test_resolve_falls_back_to_barcode_when_sei_missing(self) -> None:
        with patch.object(rtsp_timeline, "sample_sei_from_rtsp", return_value=None):
            with patch.object(rtsp_timeline, "sample_timestamp_ms_from_rtsp", return_value=1_774_328_748_518):
                resolved = resolve_recording_video_start_ts("rtsp://127.0.0.1:18554/live")
        self.assertEqual(resolved.timestamp_ms, 1_774_328_748_518)
        self.assertEqual(resolved.source, "rtsp_time_barcode")

    def test_resolve_barcode_parser_skips_sei(self) -> None:
        with patch.object(rtsp_timeline, "RTSP_TIMELINE_PARSER", "barcode"):
            with patch.object(rtsp_timeline, "sample_sei_from_rtsp") as sei_probe:
                with patch.object(rtsp_timeline, "sample_timestamp_ms_from_rtsp", return_value=1_774_328_748_518):
                    resolved = resolve_recording_video_start_ts("rtsp://127.0.0.1:18554/live")
        sei_probe.assert_not_called()
        self.assertEqual(resolved.source, "rtsp_time_barcode")

    def test_resolve_falls_back_to_map_origin(self) -> None:
        with patch.object(rtsp_timeline, "sample_sei_from_rtsp", return_value=None):
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

    def test_evidence_jpeg_gets_matching_json_sidecar(self) -> None:
        from app.services.evidence import _write_evidence_frame_metadata

        pictures = (
            PictureMetadata(
                frame_index=7,
                metadata=FrameMetadata(timestamp_ns=1_774_328_748_518_000_000, x=4.0, y=5.0, yaw=0.1),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            image = Path(tmp_dir) / "frame_1774328748518.jpg"
            image.write_bytes(b"jpeg")
            _write_evidence_frame_metadata(image, timestamp_ms=1_774_328_748_518, pictures=pictures)
            sidecar = image.with_suffix(".json")
            self.assertTrue(sidecar.is_file())
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["image"], "frame_1774328748518.jpg")
            self.assertEqual(payload["frame_index"], 7)
            self.assertEqual(payload["match"], "exact")
            self.assertAlmostEqual(payload["x"], 4.0, places=5)


if __name__ == "__main__":
    unittest.main()
