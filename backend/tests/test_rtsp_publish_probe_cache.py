from __future__ import annotations

import runpy
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services import rtsp_recorder


class RtspPublishProbeCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        rtsp_recorder.clear_rtsp_publish_probe_cache()

    def tearDown(self) -> None:
        rtsp_recorder.clear_rtsp_publish_probe_cache()

    def test_publish_probe_reuses_ttl_cache(self) -> None:
        with patch("app.services.rtsp_recorder._probe_rtsp_stream_publishing", return_value=True) as probe_mock:
            first = rtsp_recorder.is_rtsp_stream_publishing("rtsp://127.0.0.1:18554/live")
            second = rtsp_recorder.is_rtsp_stream_publishing("rtsp://127.0.0.1:18554/live")
        self.assertTrue(first)
        self.assertTrue(second)
        probe_mock.assert_called_once()

    def test_publish_probe_defaults_to_one_minute_timeout(self) -> None:
        with patch("app.services.rtsp_recorder._probe_rtsp_stream_publishing", return_value=True) as probe_mock:
            result = rtsp_recorder.is_rtsp_stream_publishing("rtsp://127.0.0.1:18554/live")

        self.assertTrue(result)
        probe_mock.assert_called_once_with(
            "rtsp://127.0.0.1:18554/live",
            timeout=60.0,
        )

    def test_publish_probe_coalesces_concurrent_calls(self) -> None:
        release = threading.Event()
        call_count = 0

        def slow_probe(_url: str, *, timeout: float = 1.5) -> bool:
            nonlocal call_count
            call_count += 1
            release.wait(timeout=2.0)
            return True

        results: list[bool] = []

        def worker() -> None:
            results.append(rtsp_recorder.is_rtsp_stream_publishing("rtsp://127.0.0.1:18554/live"))

        with patch("app.services.rtsp_recorder._probe_rtsp_stream_publishing", side_effect=slow_probe):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            time.sleep(0.05)
            release.set()
            for thread in threads:
                thread.join(timeout=3.0)

        self.assertEqual(call_count, 1)
        self.assertEqual(results, [True, True, True, True])

    def test_publish_probe_cache_expires(self) -> None:
        with patch("app.services.rtsp_recorder.RTSP_PUBLISH_CACHE_TTL_SECONDS", 0.05):
            with patch("app.services.rtsp_recorder._probe_rtsp_stream_publishing", side_effect=[True, False]) as probe_mock:
                first = rtsp_recorder.is_rtsp_stream_publishing("rtsp://127.0.0.1:18554/live")
                time.sleep(0.08)
                second = rtsp_recorder.is_rtsp_stream_publishing("rtsp://127.0.0.1:18554/live")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(probe_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
