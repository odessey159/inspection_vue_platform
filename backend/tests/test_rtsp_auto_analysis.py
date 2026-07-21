from __future__ import annotations

import runpy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.models import Project
from app.services import rtsp_auto_analysis


class RtspAutoAnalysisTest(unittest.TestCase):
    def tearDown(self) -> None:
        rtsp_auto_analysis._inflight_project_ids.clear()

    def test_rtsp_auto_analysis_mode_provider_yolo_when_monitor_llm_disabled(self) -> None:
        with patch("app.services.rtsp_auto_analysis.RTSP_WATCH_AUTO_ANALYSIS_MODE", "provider_yolo"):
            with patch("app.settings.RTSP_YOLO_MONITOR_LLM_ENABLED", False):
                self.assertEqual(rtsp_auto_analysis.rtsp_auto_analysis_mode(), "provider_yolo")

    def test_rtsp_auto_analysis_mode_disabled_when_monitor_llm_enabled(self) -> None:
        with patch("app.services.rtsp_auto_analysis.RTSP_WATCH_AUTO_ANALYSIS_MODE", "provider_yolo"):
            with patch("app.settings.RTSP_YOLO_MONITOR_LLM_ENABLED", True):
                self.assertIsNone(rtsp_auto_analysis.rtsp_auto_analysis_mode())

    def test_rtsp_auto_analysis_mode_disabled_when_empty(self) -> None:
        with patch("app.services.rtsp_auto_analysis.RTSP_WATCH_AUTO_ANALYSIS_MODE", ""):
            with patch("app.settings.RTSP_YOLO_MONITOR_LLM_ENABLED", False):
                self.assertIsNone(rtsp_auto_analysis.rtsp_auto_analysis_mode())

    def test_project_accepts_auto_analysis_when_no_prior_mode(self) -> None:
        project = Project(
            name="rtsp",
            status="indexed",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="/tmp/project",
            scene_path="scenes/scene.json",
        )
        with patch("app.services.rtsp_auto_analysis.read_analysis_summary", return_value={}):
            self.assertTrue(rtsp_auto_analysis.project_accepts_auto_analysis(project, "provider_yolo"))

    def test_project_rejects_auto_analysis_for_demo_mode_history(self) -> None:
        project = Project(
            name="rtsp",
            status="indexed",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="/tmp/project",
            scene_path="scenes/scene.json",
        )
        with patch(
            "app.services.rtsp_auto_analysis.read_analysis_summary",
            return_value={"analysis_mode": "demo"},
        ):
            self.assertFalse(rtsp_auto_analysis.project_accepts_auto_analysis(project, "provider_yolo"))

    @patch("app.services.rtsp_auto_analysis.run_analysis")
    @patch("app.services.rtsp_auto_analysis.Session")
    @patch("app.services.rtsp_auto_analysis._mark_inflight", return_value=True)
    @patch("app.services.rtsp_auto_analysis._clear_inflight")
    def test_run_auto_analysis_invokes_provider_yolo(
        self,
        _clear_mock,
        _mark_mock,
        session_cls,
        run_analysis_mock,
    ) -> None:
        project = Project(
            id=7,
            name="rtsp",
            status="indexed",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="/tmp/project",
            scene_path="scenes/scene.json",
        )
        session = session_cls.return_value.__enter__.return_value
        session.get.return_value = project

        with patch("app.services.rtsp_auto_analysis.read_analysis_summary", return_value={}):
            with patch("app.services.rtsp_auto_analysis.is_rtsp_project", return_value=True):
                rtsp_auto_analysis._run_auto_analysis_for_project(7, "provider_yolo")

        run_analysis_mock.assert_called_once()
        self.assertEqual(run_analysis_mock.call_args.kwargs["mode"], "provider_yolo")

    @patch("app.services.rtsp_auto_analysis.threading.Thread")
    def test_schedule_auto_analysis_for_rtsp_url_starts_worker(self, thread_cls) -> None:
        with patch("app.services.rtsp_auto_analysis.rtsp_auto_analysis_mode", return_value="provider_yolo"):
            rtsp_auto_analysis.schedule_auto_analysis_for_rtsp_url("rtsp://127.0.0.1:18554/live")
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()

    @patch("app.services.rtsp_watchdog.schedule_auto_analysis_for_rtsp_url")
    @patch("app.services.rtsp_watchdog.start_rtsp_yolo_monitor")
    def test_watchdog_poll_starts_auto_analysis_on_stream_connect(self, yolo_monitor_mock, schedule_mock) -> None:
        from app.services import rtsp_watchdog

        rtsp_watchdog._active_by_key.clear()
        rtsp_watchdog._live_analysis_scheduled_keys.clear()
        rtsp_watchdog._reserving_urls.clear()
        with patch("app.services.rtsp_watchdog.is_rtsp_watch_test_mode", return_value=True):
            with patch("app.services.rtsp_watchdog.prune_oldest_storage_key_recording_if_over_limit"):
                with patch(
                    "app.services.rtsp_watchdog.resolve_recording_rtsp_url",
                    return_value="rtsp://127.0.0.1:18554/live",
                ):
                    with patch("app.services.rtsp_watchdog.is_rtsp_stream_publishing", return_value=True):
                        with patch("app.services.rtsp_watchdog.acquire_stream_recording_lock", return_value=Path("lock")):
                            with patch("app.services.rtsp_watchdog.release_stream_recording_lock"):
                                with patch("app.services.rtsp_watchdog.spawn_record_rtsp_until_disconnect") as spawn_mock:
                                    spawn_mock.return_value.poll.return_value = None
                                    rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")
        yolo_monitor_mock.assert_called_once_with("local-demo", "rtsp://127.0.0.1:18554/live")
        schedule_mock.assert_called_once_with("rtsp://127.0.0.1:18554/live")

    @patch("app.services.rtsp_watchdog.schedule_auto_analysis_for_rtsp_url")
    def test_watchdog_does_not_reschedule_analysis_for_same_active_session(self, schedule_mock) -> None:
        from app.services import rtsp_watchdog

        rtsp_watchdog._active_by_key.clear()
        rtsp_watchdog._live_analysis_scheduled_keys.clear()
        rtsp_watchdog._reserving_urls.clear()
        process = mock.Mock()
        process.poll.return_value = None
        session = rtsp_watchdog._ActiveSession(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("recording.mp4"),
            process=process,
            started_at=datetime.now(timezone.utc),
        )
        rtsp_watchdog._active_by_key["local-demo"] = session
        rtsp_watchdog._live_analysis_scheduled_keys.add("local-demo")

        with patch("app.services.rtsp_watchdog.is_rtsp_watch_test_mode", return_value=True):
            rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")

        schedule_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
