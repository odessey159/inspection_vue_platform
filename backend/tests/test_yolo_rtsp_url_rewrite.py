from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.rtsp_recorder import resolve_yolo_client_rtsp_url


class ResolveYoloClientRtspUrlTest(unittest.TestCase):
    def test_rewrites_docker_internal_when_yolo_runs_on_host(self) -> None:
        with patch("app.services.rtsp_recorder.YOLO_API_URL", "http://host.docker.internal:8001"):
            self.assertEqual(
                resolve_yolo_client_rtsp_url("rtsp://host.docker.internal:18554/live"),
                "rtsp://127.0.0.1:18554/live",
            )

    def test_keeps_docker_internal_when_yolo_is_another_container(self) -> None:
        with patch("app.services.rtsp_recorder.YOLO_API_URL", "http://yolo:8001"):
            self.assertEqual(
                resolve_yolo_client_rtsp_url("rtsp://host.docker.internal:18554/live"),
                "rtsp://host.docker.internal:18554/live",
            )

    def test_leaves_loopback_unchanged(self) -> None:
        with patch("app.services.rtsp_recorder.YOLO_API_URL", "http://127.0.0.1:8001"):
            self.assertEqual(
                resolve_yolo_client_rtsp_url("rtsp://127.0.0.1:18554/live"),
                "rtsp://127.0.0.1:18554/live",
            )


if __name__ == "__main__":
    unittest.main()
