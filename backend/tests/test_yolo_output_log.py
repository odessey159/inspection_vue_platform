from __future__ import annotations

import runpy
import tempfile
import unittest
import unittest.mock
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from yolo_service.output_log import build_detection_log_text, write_detection_log


class YoloOutputLogTest(unittest.TestCase):
    def test_write_detection_log_creates_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir) / "YOLO_log"
            with unittest.mock.patch("yolo_service.output_log.YOLO_LOG_DIR", log_dir):
                log_path = write_detection_log(
                    source="rtsp",
                    clip_index="rtsp_live_01",
                    detections=[
                        {
                            "class_name": "powerbox",
                            "confidence": 0.91,
                            "time_sec": 1.5,
                            "bbox": [10.0, 20.0, 100.0, 200.0],
                            "camera_view": "rear",
                        }
                    ],
                    notes=["segment_index=1", "detections=1"],
                    extra={"segment_index": 1, "segment_start_sec": 60.0},
                )

            self.assertTrue(log_path.is_file())
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("=== YOLO Detection Output ===", content)
            self.assertIn("class=powerbox", content)
            self.assertIn("view=rear", content)
            self.assertIn("payload_json:", content)

    def test_build_detection_log_text_includes_metadata(self) -> None:
        content = build_detection_log_text(
            source="video",
            clip_index="clip-0",
            detections=[],
            notes=["clip_index=0"],
            extra={"filename": "clip.mp4"},
        )
        self.assertIn("source: video", content)
        self.assertIn("filename: clip.mp4", content)


if __name__ == "__main__":
    unittest.main()
