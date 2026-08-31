from __future__ import annotations

import runpy
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.provider_YOLO import _parse_yolo_payload


class YoloServiceContractTest(unittest.TestCase):
    def test_parse_yolo_payload_accepts_service_response(self) -> None:
        payload = {
            "detections": [
                {
                    "class_name": "powerbox",
                    "confidence": 0.91,
                    "time_sec": 1.5,
                    "bbox": [10.0, 20.0, 100.0, 200.0],
                    "camera_view": "front",
                },
                {
                    "class_name": "human",
                    "confidence": 0.12,
                    "time_sec": 2.0,
                    "bbox": [1.0, 2.0, 3.0, 4.0],
                },
            ],
            "notes": ["clip_index=0", "imgsz=960"],
        }

        result = _parse_yolo_payload(payload)

        self.assertEqual(len(result.detections), 1)
        self.assertEqual(result.detections[0].class_name, "powerbox")
        self.assertEqual(result.detections[0].camera_view, "front")
        self.assertEqual(result.notes, ["clip_index=0", "imgsz=960"])


if __name__ == "__main__":
    unittest.main()
