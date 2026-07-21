from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from fastapi.testclient import TestClient

from yolo_service.app import app, get_detector
from yolo_service.detector import VideoDetection, YoloVideoDetector


class YoloRtspServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        get_detector.cache_clear()

    def tearDown(self) -> None:
        get_detector.cache_clear()

    def test_predict_rtsp_rejects_non_rtsp_url(self) -> None:
        with patch("yolo_service.app.get_detector") as mock_get_detector:
            mock_get_detector.return_value = MagicMock()
            client = TestClient(app)
            response = client.post(
                "/predict/rtsp",
                json={"rtsp_url": "http://127.0.0.1:18554/live"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("rtsp://", response.json()["detail"])

    def test_predict_rtsp_returns_segment_response(self) -> None:
        detector = MagicMock()
        detector.detect_rtsp.return_value = (
            [
                VideoDetection(
                    class_name="powerbox",
                    confidence=0.88,
                    time_sec=1.2,
                    bbox=[1.0, 2.0, 3.0, 4.0],
                )
            ],
            ["source=rtsp", "frames=30", "detections=1"],
        )

        with patch("yolo_service.app.get_detector", return_value=detector):
            client = TestClient(app)
            response = client.post(
                "/predict/rtsp",
                json={
                    "rtsp_url": "rtsp://127.0.0.1:18554/live",
                    "duration_sec": 5,
                    "clip_index": "2",
                    "rtsp_transport": "tcp",
                    "segment_index": 1,
                    "segment_start_sec": 60,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["detections"][0]["class_name"], "powerbox")
        self.assertEqual(payload["segment_index"], 1)
        self.assertEqual(payload["segment_start_sec"], 60.0)
        self.assertEqual(payload["segment_duration_sec"], 5.0)
        self.assertEqual(payload["notes"][0], "clip_index=2")
        detector.detect_rtsp.assert_called_once_with(
            "rtsp://127.0.0.1:18554/live",
            duration_sec=5.0,
            rtsp_transport="tcp",
        )

    def test_predict_rtsp_rejects_duration_above_segment_limit(self) -> None:
        with patch("yolo_service.app.get_detector") as mock_get_detector:
            mock_get_detector.return_value = MagicMock()
            client = TestClient(app)
            response = client.post(
                "/predict/rtsp",
                json={
                    "rtsp_url": "rtsp://127.0.0.1:18554/live",
                    "duration_sec": 120,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("segment", response.json()["detail"].lower())

    def test_detect_rtsp_reads_frames_until_duration(self) -> None:
        detector = YoloVideoDetector.__new__(YoloVideoDetector)
        detector._model = MagicMock()
        detector._model.names = {0: "powerbox"}

        fake_box = MagicMock()
        fake_box.cls = [0]
        fake_box.conf = [0.9]
        fake_box.xyxy = [MagicMock()]
        fake_box.xyxy[0].tolist.return_value = [10.0, 20.0, 30.0, 40.0]

        fake_result = MagicMock()
        fake_result.boxes = [fake_box]
        fake_result.names = {0: "powerbox"}
        detector._model.predict.return_value = [fake_result]

        frames = [
            (True, object()),
            (True, object()),
            (False, None),
        ]

        capture = MagicMock()
        capture.isOpened.return_value = True
        capture.get.return_value = 25.0
        capture.read.side_effect = frames

        with patch("yolo_service.detector.cv2.VideoCapture", return_value=capture), patch(
            "yolo_service.detector.time.monotonic",
            side_effect=[0.0, 0.3, 0.6, 0.9],
        ):
            detections, notes = detector._detect_rtsp_unlocked(
                "rtsp://127.0.0.1:18554/live",
                duration_sec=1.0,
                rtsp_transport="tcp",
            )

        self.assertEqual(len(detections), 2)
        self.assertEqual(detections[0].class_name, "powerbox")
        self.assertIn("source=rtsp", notes)
        capture.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
