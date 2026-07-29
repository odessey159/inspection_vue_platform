from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services import rtsp_recorder, rtsp_watchdog
from app.services.rtsp_timeline import RtspTimelineSample


_FAKE_RTSP_TIMELINE = RtspTimelineSample(timestamp_ms=1_774_328_748_518, source="rtsp_time_barcode")


def _patch_rtsp_timeline_resolve():
    return patch(
        "app.services.rtsp_timeline.resolve_recording_video_start_ts",
        return_value=_FAKE_RTSP_TIMELINE,
    )


class RtspRecorderHelpersTest(unittest.TestCase):
    def tearDown(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        rtsp_watchdog._live_analysis_scheduled_keys.clear()
        rtsp_watchdog._reserving_urls.clear()
        rtsp_watchdog._runtime_test_mode_override = None

    def test_build_ffmpeg_command_without_duration_records_until_disconnect(self) -> None:
        output_path = Path("out.mp4")
        command = rtsp_recorder._build_rtsp_ffmpeg_command(
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=output_path,
        )
        self.assertNotIn("-t", command)
        self.assertEqual(command[-1], "out.mp4")
        self.assertIn("-timeout", command)
        self.assertEqual(command[command.index("-timeout") + 1], str(rtsp_recorder.RTSP_FFMPEG_RW_TIMEOUT_US))
        self.assertIn("-c:v", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertIn(rtsp_recorder.RTSP_LIVE_MOVFLAGS, command)

    def test_build_ffmpeg_command_with_duration_limits_recording(self) -> None:
        command = rtsp_recorder._build_rtsp_ffmpeg_command(
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("out.mp4"),
            duration_sec=30.0,
        )
        t_index = command.index("-t")
        self.assertEqual(command[t_index + 1], "30.000")
        self.assertIn("-timeout", command)
        self.assertEqual(command[command.index("-timeout") + 1], str(rtsp_recorder.RTSP_FFMPEG_RW_TIMEOUT_US))
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertIn(rtsp_recorder.RTSP_FINISHED_MOVFLAGS, command)

    def test_build_extract_clip_command_uses_stream_copy(self) -> None:
        command = rtsp_recorder._build_extract_clip_command(
            source_path=Path("recording.mp4"),
            output_path=Path("segment.mp4"),
            start_sec=30.0,
            duration_sec=30.0,
        )
        self.assertEqual(command[command.index("-ss") + 1], "30.000")
        self.assertEqual(command[command.index("-t") + 1], "30.000")
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[-1], "segment.mp4")

    def test_capture_monitor_segment_clip_prefers_watchdog_cut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            recording = Path(tmp_dir) / "recording.mp4"
            output = Path(tmp_dir) / "segment.mp4"
            recording.write_bytes(b"fake")
            active = rtsp_watchdog.ActiveRecordingInfo(
                storage_key="local-demo",
                rtsp_url="rtsp://127.0.0.1:18554/live",
                output_path=recording,
                started_at_ms=1_000,
            )
            with patch("app.services.rtsp_watchdog.get_active_recording", return_value=active), patch(
                "app.services.rtsp_recorder.probe_recorded_video",
                return_value=(90.0, 25.0),
            ), patch(
                "app.services.rtsp_recorder.extract_video_clip",
                return_value=output,
            ) as extract_mock, patch(
                "app.services.rtsp_recorder.record_rtsp_stream",
            ) as record_mock:
                result = rtsp_recorder.capture_monitor_segment_clip(
                    storage_key="local-demo",
                    output_path=output,
                    segment_start_sec=60.0,
                    duration_sec=30.0,
                    rtsp_url="rtsp://127.0.0.1:18554/live",
                )
            self.assertEqual(result, output)
            extract_mock.assert_called_once()
            record_mock.assert_not_called()

    def test_capture_monitor_segment_clip_falls_back_to_rtsp_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "segment.mp4"
            fake_recording = rtsp_recorder.RtspRecordingResult(
                rtsp_url="rtsp://127.0.0.1:18554/live",
                output_path=output,
                playback_path=output,
                video_start_ts=1,
                video_end_ts=2,
                duration_sec=30.0,
                fps=25.0,
                frame_count=750,
            )
            with patch("app.services.rtsp_watchdog.get_active_recording", return_value=None), patch(
                "app.services.rtsp_recorder.record_rtsp_stream",
                return_value=fake_recording,
            ) as record_mock:
                result = rtsp_recorder.capture_monitor_segment_clip(
                    storage_key="local-demo",
                    output_path=output,
                    segment_start_sec=0.0,
                    duration_sec=30.0,
                    rtsp_url="rtsp://127.0.0.1:18554/live",
                )
            self.assertEqual(result, output)
            kwargs = record_mock.call_args.kwargs
            self.assertEqual(kwargs["video_codec"], "copy")
            self.assertFalse(kwargs["live_fragmented"])

    def test_resolve_storage_key_uses_vehicle_id(self) -> None:
        with patch(
            "app.services.rtsp_vehicles.load_rtsp_vehicles",
            return_value=[
                type("Vehicle", (), {"id": "local-demo", "rtsp_url": "rtsp://127.0.0.1:18554/live"})()
            ],
        ):
            storage_key = rtsp_recorder.resolve_storage_key_for_rtsp_url("rtsp://127.0.0.1:18554/live")
        self.assertEqual(storage_key, "local-demo")

    def test_find_latest_completed_recording_returns_newest_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage_dir = Path(tmp_dir) / "local-demo"
            storage_dir.mkdir(parents=True)
            older = storage_dir / "recording_20260101T000000Z.mp4"
            newer = storage_dir / "recording_20260201T000000Z.mp4"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            older.touch()
            import time
            time.sleep(0.01)
            newer.touch()

            with patch("app.services.rtsp_recorder.RTSP_RECORDINGS_DIR", Path(tmp_dir)):
                with patch(
                    "app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url",
                    return_value="local-demo",
                ):
                    latest = rtsp_recorder.find_latest_completed_recording("rtsp://127.0.0.1:18554/live")

            self.assertEqual(latest, newer)

    def test_build_rtsp_playback_state_prefers_recorded_url_after_recording(self) -> None:
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            with patch("app.services.rtsp_watchdog.is_stable_recording_active_for_rtsp_url", return_value=False):
                with patch("app.services.rtsp_recorder.is_rtsp_stream_publishing", return_value=False):
                    with patch(
                        "app.services.rtsp_recorder.find_latest_completed_recording_for_storage_key",
                        return_value=Path("recording_20260101T000000Z.mp4"),
                    ):
                        payload = rtsp_recorder.build_rtsp_playback_state(
                            "rtsp://127.0.0.1:18554/live",
                            project_id=7,
                        )
        self.assertFalse(payload["recording_active"])
        self.assertFalse(payload["stream_online"])
        self.assertEqual(payload["live_url"], "/api/projects/7/rtsp-live")
        self.assertEqual(payload["recorded_video_url"], "/api/rtsp-recordings/local-demo/latest")
        self.assertIsNone(payload["live_video_start_ts"])
        self.assertGreater(int(payload["recorded_video_start_ts"] or 0), 0)

    def test_build_rtsp_playback_state_exposes_active_session_start(self) -> None:
        active = rtsp_watchdog.ActiveRecordingInfo(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("recording.mp4"),
            started_at_ms=1_775_000_000_000,
        )
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            with patch("app.services.rtsp_watchdog.is_stable_recording_active_for_rtsp_url", return_value=True):
                with patch("app.services.rtsp_watchdog.get_active_recording", return_value=active):
                    with patch(
                        "app.services.rtsp_recorder.find_latest_completed_recording_for_storage_key",
                        return_value=None,
                    ):
                        payload = rtsp_recorder.build_rtsp_playback_state(
                            "rtsp://127.0.0.1:18554/live",
                            project_id=7,
                        )

        self.assertEqual(payload["live_video_start_ts"], active.started_at_ms)
        self.assertIsNone(payload["recorded_video_start_ts"])

    def test_build_rtsp_playback_state_marks_stream_online_without_active_recording(self) -> None:
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            with patch("app.services.rtsp_watchdog.is_stable_recording_active_for_rtsp_url", return_value=False):
                with patch("app.services.rtsp_recorder.is_rtsp_stream_publishing", return_value=True):
                    with patch(
                        "app.services.rtsp_recorder.find_latest_completed_recording_for_storage_key",
                        return_value=Path("recording.mp4"),
                    ):
                        payload = rtsp_recorder.build_rtsp_playback_state("rtsp://127.0.0.1:18554/live")
        self.assertFalse(payload["recording_active"])
        self.assertTrue(payload["stream_online"])

    def test_build_rtsp_playback_state_skips_publish_probe_while_recording(self) -> None:
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            with patch("app.services.rtsp_watchdog.is_stable_recording_active_for_rtsp_url", return_value=True):
                with patch("app.services.rtsp_recorder.is_rtsp_stream_publishing") as publish_mock:
                    with patch(
                        "app.services.rtsp_recorder.find_latest_completed_recording_for_storage_key",
                        return_value=None,
                    ):
                        payload = rtsp_recorder.build_rtsp_playback_state("rtsp://127.0.0.1:18554/live")
        publish_mock.assert_not_called()
        self.assertTrue(payload["recording_active"])
        self.assertTrue(payload["stream_online"])

    def test_build_rtsp_playback_state_ignores_unstable_recording(self) -> None:
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            with patch("app.services.rtsp_watchdog.is_stable_recording_active_for_rtsp_url", return_value=False):
                with patch("app.services.rtsp_recorder.is_rtsp_stream_publishing", return_value=False):
                    with patch(
                        "app.services.rtsp_recorder.find_latest_completed_recording_for_storage_key",
                        return_value=Path("recording.mp4"),
                    ):
                        payload = rtsp_recorder.build_rtsp_playback_state("rtsp://127.0.0.1:18554/live")
        self.assertFalse(payload["recording_active"])
        self.assertFalse(payload["stream_online"])

    def test_storage_key_has_recordings_detects_existing_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage_dir = Path(tmp_dir) / "local-demo"
            storage_dir.mkdir(parents=True)
            (storage_dir / "recording_20260101T000000Z.mp4").write_bytes(b"video")

            with patch("app.services.rtsp_recorder.RTSP_RECORDINGS_DIR", Path(tmp_dir)):
                self.assertTrue(rtsp_recorder.storage_key_has_recordings("local-demo"))
                self.assertFalse(rtsp_recorder.storage_key_has_recordings("missing"))

    def test_clear_storage_key_recordings_removes_existing_videos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage_dir = Path(tmp_dir) / "local-demo"
            storage_dir.mkdir(parents=True)
            recording = storage_dir / "recording_20260101T000000Z.mp4"
            recording.write_bytes(b"video")

            with patch("app.services.rtsp_recorder.RTSP_RECORDINGS_DIR", Path(tmp_dir)):
                result = rtsp_recorder.clear_storage_key_recordings("local-demo")

            self.assertEqual(result["deleted_files"], 1)
            self.assertFalse(recording.exists())
            self.assertFalse(storage_dir.exists())

    def test_prune_oldest_storage_key_recording_only_when_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage_dir = Path(tmp_dir) / "local-demo"
            storage_dir.mkdir(parents=True)
            paths = []
            for index in range(5):
                path = storage_dir / f"recording_2026010{index}T000000Z.mp4"
                path.write_bytes(b"x")
                paths.append(path)

            with patch("app.services.rtsp_recorder.RTSP_RECORDINGS_DIR", Path(tmp_dir)):
                unchanged = rtsp_recorder.prune_oldest_storage_key_recording_if_over_limit("local-demo", max_recordings=5)
                self.assertEqual(unchanged["deleted_files"], 0)
                self.assertEqual(len(rtsp_recorder.list_storage_key_recordings("local-demo")), 5)

                sixth = storage_dir / "recording_20260105T000000Z.mp4"
                sixth.write_bytes(b"y")
                paths.append(sixth)

                import os
                import time

                base = time.time()
                for index, path in enumerate(paths):
                    # Make paths[0] strictly oldest so prune deletes it first.
                    stamp = base - (len(paths) - index)
                    os.utime(path, (stamp, stamp))

                pruned = rtsp_recorder.prune_oldest_storage_key_recording_if_over_limit("local-demo", max_recordings=5)
                remaining = rtsp_recorder.list_storage_key_recordings("local-demo")

            self.assertEqual(pruned["deleted_files"], 1)
            self.assertFalse(paths[0].exists())
            self.assertEqual(len(remaining), 5)
            self.assertNotIn(paths[0], remaining)

    def test_poll_vehicle_prunes_oldest_recording_in_test_mode(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        rtsp_watchdog._reserving_urls.clear()
        with patch("app.services.rtsp_watchdog.start_rtsp_yolo_monitor"):
            with patch("app.services.rtsp_watchdog.is_rtsp_watch_test_mode", return_value=True):
                with patch("app.services.rtsp_watchdog.prune_oldest_storage_key_recording_if_over_limit") as prune_mock:
                    with patch(
                        "app.services.rtsp_watchdog.resolve_recording_rtsp_url",
                        return_value="rtsp://127.0.0.1:18554/live",
                    ):
                        with patch("app.services.rtsp_watchdog.is_rtsp_stream_publishing", return_value=True):
                            with patch("app.services.rtsp_watchdog.acquire_stream_recording_lock", return_value=Path("lock")):
                                with patch("app.services.rtsp_watchdog.release_stream_recording_lock"):
                                    with patch("app.services.rtsp_watchdog.spawn_record_rtsp_until_disconnect") as spawn_mock:
                                        spawn_mock.return_value.poll.return_value = None
                                        with _patch_rtsp_timeline_resolve():
                                            with patch("app.services.rtsp_timeline.write_recording_timeline_meta"):
                                                rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")
        prune_mock.assert_called_once_with("local-demo", max_recordings=5)
        spawn_mock.assert_called_once()

    def test_poll_vehicle_uses_test_mode_duration_cap(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        rtsp_watchdog._reserving_urls.clear()
        with patch("app.services.rtsp_watchdog.start_rtsp_yolo_monitor"):
            with patch("app.services.rtsp_watchdog.is_rtsp_watch_test_mode", return_value=True):
                with patch("app.services.rtsp_watchdog.RTSP_WATCH_TEST_MAX_SECONDS", 600.0):
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
                                            with _patch_rtsp_timeline_resolve():
                                                with patch("app.services.rtsp_timeline.write_recording_timeline_meta"):
                                                    rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")
        spawn_mock.assert_called_once()
        self.assertEqual(spawn_mock.call_args.kwargs["max_duration_sec"], 600.0)

    def test_poll_vehicle_skips_when_port_open_but_not_publishing(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        rtsp_watchdog._reserving_urls.clear()
        with patch("app.services.rtsp_watchdog.is_rtsp_watch_test_mode", return_value=True):
            with patch("app.services.rtsp_watchdog.prune_oldest_storage_key_recording_if_over_limit"):
                with patch(
                    "app.services.rtsp_watchdog.resolve_recording_rtsp_url",
                    return_value="rtsp://127.0.0.1:18554/live",
                ):
                    with patch("app.services.rtsp_watchdog.is_rtsp_stream_publishing", return_value=False):
                        with patch("app.services.rtsp_watchdog.spawn_record_rtsp_until_disconnect") as spawn_mock:
                            rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")
        spawn_mock.assert_not_called()
        self.assertEqual(rtsp_watchdog._active_by_key, {})

    def test_runtime_test_mode_toggle_overrides_env_default(self) -> None:
        with patch("app.services.rtsp_watchdog.RTSP_WATCH_TEST_MODE", False):
            rtsp_watchdog.set_rtsp_watch_test_mode(True)
            try:
                self.assertTrue(rtsp_watchdog.is_rtsp_watch_test_mode())
                self.assertTrue(rtsp_watchdog.rtsp_watch_settings_payload()["test_mode"])
            finally:
                rtsp_watchdog._runtime_test_mode_override = None

    def test_stream_recording_lock_prevents_second_owner(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("app.services.rtsp_recorder.RTSP_RECORDINGS_DIR", Path(tmp_dir)):
                first = rtsp_recorder.acquire_stream_recording_lock("rtsp://127.0.0.1:18554/live")
                second = rtsp_recorder.acquire_stream_recording_lock("rtsp://127.0.0.1:18554/live")
                self.assertIsNotNone(first)
                self.assertIsNone(second)
                rtsp_recorder.release_stream_recording_lock(first)

    def test_recording_active_matches_session_by_storage_key(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        process = type("Process", (), {"poll": lambda self: None})()
        rtsp_watchdog._active_by_key["vehicle-05"] = rtsp_watchdog._ActiveSession(
            storage_key="vehicle-05",
            rtsp_url="rtsp://host.docker.internal:18554/live",
            output_path=Path("recording.mp4"),
            process=process,
            started_at=rtsp_watchdog.datetime.now(rtsp_watchdog.timezone.utc),
        )
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="vehicle-05"):
            self.assertTrue(rtsp_watchdog.is_recording_active_for_rtsp_url("rtsp://127.0.0.1:18554/live"))

    def test_recording_active_matches_localhost_fallback_url(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        process = type("Process", (), {"poll": lambda self: None})()
        rtsp_watchdog._active_by_key["local-demo"] = rtsp_watchdog._ActiveSession(
            storage_key="local-demo",
            rtsp_url="rtsp://host.docker.internal:18554/live",
            output_path=Path("recording.mp4"),
            process=process,
            started_at=rtsp_watchdog.datetime.now(rtsp_watchdog.timezone.utc),
        )
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="other-key"):
            self.assertTrue(rtsp_watchdog.is_recording_active_for_rtsp_url("rtsp://127.0.0.1:18554/live"))

    @patch("app.services.rtsp_watchdog.schedule_auto_analysis_for_rtsp_url")
    @patch("app.services.rtsp_watchdog.start_rtsp_yolo_monitor")
    def test_schedule_live_analysis_starts_continuous_yolo_monitor(self, yolo_monitor_mock, auto_analysis_mock) -> None:
        process = type("Process", (), {"poll": lambda self: None})()
        session = rtsp_watchdog._ActiveSession(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("recording.mp4"),
            process=process,
            started_at=rtsp_watchdog.datetime.now(rtsp_watchdog.timezone.utc),
        )
        rtsp_watchdog._schedule_live_analysis_on_connect(session)

        yolo_monitor_mock.assert_called_once_with("local-demo", "rtsp://127.0.0.1:18554/live")
        auto_analysis_mock.assert_called_once_with("rtsp://127.0.0.1:18554/live")

    @patch("app.services.rtsp_watchdog.start_rtsp_yolo_monitor")
    def test_poll_vehicle_keeps_monitor_running_for_existing_session(self, yolo_monitor_mock) -> None:
        process = type("Process", (), {"poll": lambda self: None})()
        rtsp_watchdog._active_by_key["local-demo"] = rtsp_watchdog._ActiveSession(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("recording.mp4"),
            process=process,
            started_at=rtsp_watchdog.datetime.now(rtsp_watchdog.timezone.utc),
        )

        rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")

        yolo_monitor_mock.assert_called_once_with("local-demo", "rtsp://127.0.0.1:18554/live")

    @patch("app.services.rtsp_watchdog.schedule_auto_analysis_for_rtsp_url")
    def test_poll_vehicle_skips_when_stream_lock_is_held(self, _schedule_mock) -> None:
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
                        with patch("app.services.rtsp_watchdog.acquire_stream_recording_lock", return_value=None):
                            with patch("app.services.rtsp_watchdog.spawn_record_rtsp_until_disconnect") as spawn_mock:
                                rtsp_watchdog._poll_vehicle("local-demo", "rtsp://127.0.0.1:18554/live")
        spawn_mock.assert_not_called()

    def test_stable_recording_ignores_brand_new_empty_session(self) -> None:
        rtsp_watchdog._active_by_key.clear()
        process = type("Process", (), {"poll": lambda self: None})()
        rtsp_watchdog._active_by_key["local-demo"] = rtsp_watchdog._ActiveSession(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("missing-recording.mp4"),
            process=process,
            started_at=rtsp_watchdog.datetime.now(rtsp_watchdog.timezone.utc),
        )
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            self.assertTrue(rtsp_watchdog.is_recording_active_for_rtsp_url("rtsp://127.0.0.1:18554/live"))
            self.assertFalse(rtsp_watchdog.is_stable_recording_active_for_rtsp_url("rtsp://127.0.0.1:18554/live"))

    def test_stable_recording_accepts_aged_session(self) -> None:
        from datetime import timedelta

        rtsp_watchdog._active_by_key.clear()
        process = type("Process", (), {"poll": lambda self: None})()
        started_at = rtsp_watchdog.datetime.now(rtsp_watchdog.timezone.utc) - timedelta(seconds=3)
        rtsp_watchdog._active_by_key["local-demo"] = rtsp_watchdog._ActiveSession(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            output_path=Path("missing-recording.mp4"),
            process=process,
            started_at=started_at,
        )
        with patch("app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url", return_value="local-demo"):
            self.assertTrue(rtsp_watchdog.is_stable_recording_active_for_rtsp_url("rtsp://127.0.0.1:18554/live"))


if __name__ == "__main__":
    unittest.main()
