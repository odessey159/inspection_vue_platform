from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from fastapi.testclient import TestClient

from yolo_service.app import app, get_detector
from yolo_service.detector import VideoDetection, YoloVideoDetector
from yolo_service.frame_layout import env_frame_layout


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
                    camera_view="front",
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
        self.assertEqual(payload["detections"][0]["camera_view"], "front")
        self.assertEqual(payload["segment_index"], 1)
        self.assertEqual(payload["segment_start_sec"], 60.0)
        self.assertEqual(payload["segment_duration_sec"], 5.0)
        self.assertEqual(payload["notes"][0], "clip_index=2")
        detector.detect_rtsp.assert_called_once_with(
            "rtsp://127.0.0.1:18554/live",
            duration_sec=5.0,
            rtsp_transport="tcp",
            frame_layout=env_frame_layout(),
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
                layout="full",
            )

        self.assertEqual(len(detections), 2)
        self.assertEqual(detections[0].class_name, "powerbox")
        self.assertIn("source=rtsp", notes)
        capture.release.assert_called_once()
        self.assertEqual(detector._model.predict.call_count, 2)

    def test_predict_rtsp_forwards_full_frame_layout(self) -> None:
        detector = MagicMock()
        detector.detect_rtsp.return_value = ([], ["frame_layout=full"])

        with patch("yolo_service.app.get_detector", return_value=detector):
            client = TestClient(app)
            response = client.post(
                "/predict/rtsp",
                json={
                    "rtsp_url": "rtsp://127.0.0.1:18554/live",
                    "duration_sec": 5,
                    "frame_layout": "full",
                },
            )

        self.assertEqual(response.status_code, 200)
        detector.detect_rtsp.assert_called_once()
        self.assertEqual(detector.detect_rtsp.call_args.kwargs["frame_layout"], "full")

    def test_detect_rtsp_quad_runs_predict_per_tile_and_offsets_bbox(self) -> None:
        detector = YoloVideoDetector.__new__(YoloVideoDetector)
        detector._model = MagicMock()
        detector._model.names = {0: "powerbox"}

        fake_box = MagicMock()
        fake_box.cls = [0]
        fake_box.conf = [0.9]
        fake_box.xyxy = [MagicMock()]
        fake_box.xyxy[0].tolist.return_value = [1.0, 2.0, 3.0, 4.0]

        fake_result = MagicMock()
        fake_result.boxes = [fake_box]
        fake_result.names = {0: "powerbox"}
        detector._model.predict.return_value = [fake_result]

        frame = _FakeFrame(40, 80)
        capture = MagicMock()
        capture.isOpened.return_value = True
        capture.get.return_value = 25.0
        capture.read.side_effect = [(True, frame), (False, None)]

        with patch("yolo_service.detector.cv2.VideoCapture", return_value=capture), patch(
            "yolo_service.detector.time.monotonic",
            side_effect=[0.0, 0.1, 0.2],
        ):
            detections, notes = detector._detect_rtsp_unlocked(
                "rtsp://127.0.0.1:18554/live",
                duration_sec=1.0,
                rtsp_transport="tcp",
                layout="quad",
            )

        self.assertEqual(detector._model.predict.call_count, 4)
        self.assertEqual(len(detections), 4)
        self.assertEqual([item.camera_view for item in detections], ["front", "rear", "left", "right"])
        self.assertEqual(detections[0].bbox, [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(detections[1].bbox, [41.0, 2.0, 43.0, 4.0])
        self.assertEqual(detections[2].bbox, [1.0, 22.0, 3.0, 24.0])
        self.assertEqual(detections[3].bbox, [41.0, 22.0, 43.0, 24.0])
        self.assertIn("frame_layout=quad", notes)


class _FakeFrame:
    def __init__(self, height: int, width: int) -> None:
        self.shape = (height, width, 3)

    def __getitem__(self, key):
        y_key, x_key = key[0], key[1]
        y0, y1, _ = y_key.indices(self.shape[0])
        x0, x1, _ = x_key.indices(self.shape[1])
        return _FakeFrame(y1 - y0, x1 - x0)


if __name__ == "__main__":
    unittest.main()
