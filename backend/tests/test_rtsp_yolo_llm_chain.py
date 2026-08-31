from __future__ import annotations

import runpy
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Path is used by monitor wiring tests for ActiveRecordingInfo stubs.

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.models import HazardRule, Project
from app.services.analysis_types import FindingSeed
from app.services.provider import ProviderResponsePayload
from app.services.provider_YOLO import YoloClipResult, YoloDetectionPayload
from app.services import rtsp_yolo_llm_chain
from app.services import rtsp_yolo_monitor


class RtspYoloLlmChainTest(unittest.TestCase):
    def tearDown(self) -> None:
        rtsp_yolo_llm_chain._review_inflight.clear()

    def test_should_review_segment_requires_detections_by_default(self) -> None:
        empty = YoloClipResult(detections=[], notes=[])
        with_detections = YoloClipResult(
            detections=[YoloDetectionPayload(class_name="person", confidence=0.9, time_sec=1.0)],
            notes=[],
        )
        with patch("app.services.rtsp_yolo_llm_chain.RTSP_YOLO_MONITOR_LLM_ENABLED", True):
            with patch("app.services.rtsp_yolo_llm_chain.provider_available", return_value=True):
                with patch("app.services.rtsp_yolo_llm_chain.RTSP_YOLO_MONITOR_LLM_ON_EMPTY", False):
                    self.assertFalse(rtsp_yolo_llm_chain.should_review_segment(empty))
                    self.assertTrue(rtsp_yolo_llm_chain.should_review_segment(with_detections))

    def test_should_review_empty_when_on_empty_enabled(self) -> None:
        empty = YoloClipResult(detections=[], notes=[])
        with patch("app.services.rtsp_yolo_llm_chain.RTSP_YOLO_MONITOR_LLM_ENABLED", True):
            with patch("app.services.rtsp_yolo_llm_chain.provider_available", return_value=True):
                with patch("app.services.rtsp_yolo_llm_chain.RTSP_YOLO_MONITOR_LLM_ON_EMPTY", True):
                    self.assertTrue(rtsp_yolo_llm_chain.should_review_segment(empty))

    def test_monitor_llm_disabled_without_provider(self) -> None:
        with patch("app.services.rtsp_yolo_llm_chain.RTSP_YOLO_MONITOR_LLM_ENABLED", True):
            with patch("app.services.rtsp_yolo_llm_chain.provider_available", return_value=False):
                self.assertFalse(rtsp_yolo_llm_chain.monitor_llm_enabled())

    def test_build_segment_review_chain_is_runnable(self) -> None:
        chain = rtsp_yolo_llm_chain.build_segment_review_chain()
        self.assertTrue(hasattr(chain, "invoke"))

    def test_chain_skips_when_no_matching_projects(self) -> None:
        yolo_result = YoloClipResult(
            detections=[YoloDetectionPayload(class_name="person", confidence=0.9, time_sec=1.0)],
            notes=[],
        )
        with patch("app.services.rtsp_yolo_llm_chain.list_rtsp_projects_for_storage_key", return_value=[]):
            with patch("app.services.rtsp_yolo_llm_chain.Session") as session_cls:
                session_cls.return_value.__enter__.return_value = MagicMock()
                state = rtsp_yolo_llm_chain.run_segment_llm_review(
                    storage_key="missing",
                    rtsp_url="rtsp://127.0.0.1:18554/live",
                    segment_index=0,
                    segment_start_sec=0.0,
                    segment_duration_sec=30.0,
                    yolo_result=yolo_result,
                )
        self.assertTrue(state.skipped)
        self.assertTrue(any("No imported RTSP project" in item for item in state.diagnostics))

    def test_invoke_llm_step_uses_langchain_chat_model(self) -> None:
        project = Project(
            id=11,
            name="rtsp",
            status="indexed",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="/tmp/project",
            scene_path="scenes/scene.json",
        )
        rule = HazardRule(
            project_id=11,
            rule_id="rule-001",
            domain="industrial-inspection",
            category="ppe",
            object_name="person",
            check_item="helmet",
            hazard_desc="Missing helmet",
            legal_basis="",
            evidence_objects_json='["person"]',
            severity="high",
            visual_detectable=True,
        )
        state = rtsp_yolo_llm_chain.SegmentReviewState(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            segment_index=1,
            segment_start_sec=30.0,
            segment_duration_sec=30.0,
            yolo_result=YoloClipResult(
                detections=[YoloDetectionPayload(class_name="person", confidence=0.88, time_sec=1.2)],
                notes=[],
            ),
            video_start_ts=1_700_000_000_000,
            selected_model="qwen3.5-plus",
            projects=[project],
            rules=[rule],
            rules_by_id={"rule-001": rule},
            retrieved_rules=[rule],
            prepared_clip=rtsp_yolo_llm_chain.PreparedClip(
                index=1,
                path=Path(""),
                start_offset_sec=30.0,
                duration_sec=30.0,
                start_ts_ms=1_700_000_000_000,
                byte_size=0,
                profile_name="text-only",
            ),
            yolo_prompt_section="=== YOLO pre-analysis context ===",
        )

        fake_llm = MagicMock()
        fake_response = MagicMock()
        fake_response.content = '{"findings":[],"notes":["reviewed"]}'
        fake_llm.invoke.return_value = fake_response
        payload = ProviderResponsePayload(findings=[], notes=["reviewed"])

        with patch("langchain_openai.ChatOpenAI", return_value=fake_llm):
            with patch(
                "app.services.rtsp_yolo_llm_chain._normalize_message_content",
                return_value={"findings": [], "notes": ["reviewed"]},
            ):
                with patch(
                    "app.services.rtsp_yolo_llm_chain.ProviderResponsePayload.model_validate",
                    return_value=payload,
                ):
                    with patch("app.services.rtsp_yolo_llm_chain._clip_findings_to_seeds", return_value=[]):
                        with patch("app.services.rtsp_yolo_llm_chain.RULE_RAG_ENABLED", False):
                            result = rtsp_yolo_llm_chain._step_invoke_llm(state)

        self.assertIsNotNone(result.provider_payload)
        fake_llm.invoke.assert_called_once()
        self.assertIn(11, result.seeds_by_project)

    def test_invoke_llm_step_dedupes_same_time_findings(self) -> None:
        project = Project(
            id=11,
            name="rtsp",
            status="indexed",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="/tmp/project",
            scene_path="scenes/scene.json",
        )
        rule = HazardRule(
            project_id=11,
            rule_id="rule-001",
            domain="industrial-inspection",
            category="ppe",
            object_name="person",
            check_item="helmet",
            hazard_desc="Missing helmet",
            legal_basis="",
            evidence_objects_json='["person"]',
            severity="high",
            visual_detectable=True,
        )
        state = rtsp_yolo_llm_chain.SegmentReviewState(
            storage_key="local-demo",
            rtsp_url="rtsp://127.0.0.1:18554/live",
            segment_index=1,
            segment_start_sec=30.0,
            segment_duration_sec=30.0,
            yolo_result=YoloClipResult(detections=[], notes=[]),
            video_start_ts=1_700_000_000_000,
            selected_model="qwen3.5-plus",
            projects=[project],
            rules=[rule],
            rules_by_id={"rule-001": rule},
            retrieved_rules=[rule],
            prepared_clip=rtsp_yolo_llm_chain.PreparedClip(
                index=1,
                path=Path(""),
                start_offset_sec=30.0,
                duration_sec=30.0,
                start_ts_ms=1_700_000_000_000,
                byte_size=0,
                profile_name="text-only",
            ),
            yolo_prompt_section="=== YOLO pre-analysis context ===",
        )
        duplicate_seeds = [
            FindingSeed(
                rule_id="rule-001",
                title="Missing helmet",
                time_start_ms=1_700_000_010_000,
                time_end_ms=1_700_000_011_000,
                evidence_frame_ts=[1_700_000_010_000],
                description="front view",
                confidence=0.55,
                severity="high",
            ),
            FindingSeed(
                rule_id="rule-001",
                title="Missing helmet",
                time_start_ms=1_700_000_010_400,
                time_end_ms=1_700_000_011_400,
                evidence_frame_ts=[1_700_000_010_400],
                description="rear view",
                confidence=0.88,
                severity="high",
            ),
        ]
        fake_llm = MagicMock()
        fake_response = MagicMock()
        fake_response.content = '{"findings":[],"notes":[]}'
        fake_llm.invoke.return_value = fake_response
        payload = ProviderResponsePayload(findings=[], notes=[])

        with patch("langchain_openai.ChatOpenAI", return_value=fake_llm):
            with patch(
                "app.services.rtsp_yolo_llm_chain._normalize_message_content",
                return_value={"findings": [], "notes": []},
            ):
                with patch(
                    "app.services.rtsp_yolo_llm_chain.ProviderResponsePayload.model_validate",
                    return_value=payload,
                ):
                    with patch(
                        "app.services.rtsp_yolo_llm_chain._clip_findings_to_seeds",
                        return_value=duplicate_seeds,
                    ):
                        with patch(
                            "app.services.rtsp_yolo_llm_chain.write_llm_log",
                            return_value=Path("llm.log"),
                        ):
                            with patch(
                                "app.services.rtsp_yolo_llm_chain.YOLO_SAME_TIME_DEDUPE_WINDOW_MS",
                                2000,
                            ):
                                result = rtsp_yolo_llm_chain._step_invoke_llm(state)

        kept = result.seeds_by_project[11]
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0].confidence, 0.88)


class RtspYoloMonitorLlmWiringTest(unittest.TestCase):
    def tearDown(self) -> None:
        rtsp_yolo_monitor._stop_by_key.clear()
        rtsp_yolo_monitor._thread_by_key.clear()

    @patch("app.services.rtsp_yolo_monitor.schedule_segment_llm_review")
    @patch("app.services.rtsp_yolo_monitor.invoke_yolo_rtsp_segment")
    @patch("app.services.rtsp_yolo_monitor.should_review_segment", return_value=True)
    @patch("app.services.rtsp_yolo_monitor.monitor_llm_enabled", return_value=False)
    def test_monitor_schedules_llm_after_yolo_segment(
        self,
        _llm_enabled,
        _should_review,
        yolo_mock,
        schedule_mock,
    ) -> None:
        yolo_mock.return_value = YoloClipResult(
            detections=[YoloDetectionPayload(class_name="person", confidence=0.8, time_sec=1.0)],
            notes=[],
        )
        stop_event = _StopAfterFirstSegment()
        active = type(
            "Active",
            (),
            {
                "storage_key": "local-demo",
                "rtsp_url": "rtsp://127.0.0.1:18554/live",
                "output_path": Path("recording.mp4"),
                "started_at_ms": 1_700_000_000_000,
            },
        )()

        with patch("app.services.rtsp_yolo_monitor.YOLO_RTSP_SEGMENT_SECONDS", 30.0):
            with patch("app.services.rtsp_watchdog.get_active_recording", return_value=active):
                rtsp_yolo_monitor._monitor_loop(
                    "local-demo",
                    "rtsp://127.0.0.1:18554/live",
                    "tcp",
                    stop_event,
                )

        yolo_mock.assert_called()
        schedule_mock.assert_called_once()
        kwargs = schedule_mock.call_args.kwargs
        self.assertEqual(kwargs["storage_key"], "local-demo")
        self.assertEqual(kwargs["segment_index"], 0)
        self.assertEqual(kwargs["timeline_origin_ms"], 1_700_000_000_000)
        self.assertEqual(kwargs["video_start_ts"], 1_700_000_000_000)
        self.assertEqual(len(kwargs["yolo_result"].detections), 1)

    @patch("app.services.rtsp_yolo_monitor.schedule_segment_llm_review")
    @patch("app.services.rtsp_yolo_monitor.invoke_yolo_rtsp_segment")
    @patch("app.services.rtsp_yolo_monitor.should_review_segment", return_value=False)
    @patch("app.services.rtsp_yolo_monitor.monitor_llm_enabled", return_value=False)
    def test_monitor_skips_llm_when_should_review_false(
        self,
        _llm_enabled,
        _should_review,
        yolo_mock,
        schedule_mock,
    ) -> None:
        yolo_mock.return_value = YoloClipResult(detections=[], notes=[])
        stop_event = _StopAfterFirstSegment()

        with patch("app.services.rtsp_yolo_monitor.YOLO_RTSP_SEGMENT_SECONDS", 1.0):
            rtsp_yolo_monitor._monitor_loop(
                "local-demo",
                "rtsp://127.0.0.1:18554/live",
                "tcp",
                stop_event,
            )

        schedule_mock.assert_not_called()


class _StopAfterFirstSegment(threading.Event):
    """Unset on the first loop check, set afterward so the monitor exits."""

    def __init__(self) -> None:
        super().__init__()
        self._checks = 0

    def is_set(self) -> bool:
        self._checks += 1
        return self._checks > 1

    def wait(self, timeout: float | None = None) -> bool:
        return True


if __name__ == "__main__":
    unittest.main()
