from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.analysis import run_analysis
from app.services.provider import PreparedClip
from app.services.provider_YOLO import (
    YoloClipResult,
    YoloDetectionPayload,
    _merge_yolo_segment_results,
    _split_yolo_result_for_clip,
)


class ProviderYoloRtspRoutingTest(unittest.TestCase):
    def test_split_yolo_result_for_clip_keeps_window_relative_times(self) -> None:
        full_result = YoloClipResult(
            detections=[
                YoloDetectionPayload(class_name="powerbox", confidence=0.9, time_sec=1.0, bbox=[1, 2, 3, 4]),
                YoloDetectionPayload(class_name="human", confidence=0.8, time_sec=26.0, bbox=[5, 6, 7, 8]),
            ],
            notes=["source=rtsp"],
            raw_payload={},
        )
        clip = PreparedClip(
            index=1,
            path=Path("clip.mp4"),
            start_offset_sec=25.0,
            duration_sec=25.0,
            start_ts_ms=25_000,
            byte_size=1024,
            profile_name="balanced-540p",
        )

        clip_result = _split_yolo_result_for_clip(full_result, clip)

        self.assertEqual(len(clip_result.detections), 1)
        self.assertEqual(clip_result.detections[0].class_name, "human")
        self.assertAlmostEqual(clip_result.detections[0].time_sec or 0.0, 1.0)

    def test_merge_yolo_segment_results_offsets_detection_times(self) -> None:
        first = YoloClipResult(
            detections=[
                YoloDetectionPayload(class_name="powerbox", confidence=0.9, time_sec=1.0, bbox=[]),
            ],
            notes=["segment-a"],
            raw_payload={"segment_index": 0},
        )
        second = YoloClipResult(
            detections=[
                YoloDetectionPayload(class_name="human", confidence=0.8, time_sec=2.0, bbox=[]),
            ],
            notes=["segment-b"],
            raw_payload={"segment_index": 1},
        )

        merged = _merge_yolo_segment_results(
            [
                (0, 0.0, 60.0, first),
                (1, 60.0, 60.0, second),
            ]
        )

        self.assertEqual(len(merged.detections), 2)
        self.assertAlmostEqual(merged.detections[0].time_sec or 0.0, 1.0)
        self.assertAlmostEqual(merged.detections[1].time_sec or 0.0, 62.0)
        self.assertEqual(merged.raw_payload.get("segment_count"), 2)

    @patch("app.services.analysis.run_provider_yolo_rtsp_live_analysis")
    @patch("app.services.analysis.should_use_rtsp_live_yolo_analysis", return_value=True)
    @patch("app.services.analysis.is_rtsp_project", return_value=True)
    @patch("app.services.analysis.load_rtsp_project_settings")
    @patch("app.services.analysis.write_analysis_summary")
    @patch("app.services.analysis.cache_evidence_frames")
    def test_run_analysis_uses_rtsp_live_yolo_when_stream_is_online(
        self,
        _cache_frames,
        _write_summary,
        mock_load_settings,
        _is_rtsp_project,
        _should_live,
        mock_live_analysis,
    ) -> None:
        from app.models import HazardRule, Project

        mock_load_settings.return_value = MagicMock(rtsp_url="rtsp://127.0.0.1:18554/live")
        mock_live_analysis.return_value = MagicMock(status="provider_analyzed", findings=[], summary={"notes": []})

        project = Project(
            id=1,
            name="rtsp-demo",
            status="indexed",
            bag_dir="rtsp://127.0.0.1:18554/live",
            artifacts_dir=str(Path("artifacts")),
            scene_path="scenes/scene.json",
            bag_start_ts=0,
            bag_end_ts=60_000,
        )
        rule = HazardRule(
            project_id=1,
            rule_id="rule-001",
            hazard_desc="Test hazard",
            severity="high",
            visual_detectable=True,
            checker_scope="visual",
            object_name="powerbox",
            check_item="check",
            evidence_objects_json="[]",
        )
        session = MagicMock()
        def _exec(statement):
            statement_text = str(statement)
            if "hazardrule" in statement_text.lower() or "HazardRule" in statement_text:
                return [rule]
            return []

        session.exec.side_effect = _exec

        with patch("app.services.analysis.read_json", return_value={"trajectory": [[0, 0, 0]], "trajectory_timestamps": [0, 60_000]}), patch(
            "app.services.analysis.resolve_project_path",
            return_value=Path("scene.json"),
        ), patch("app.services.analysis.ensure_rtsp_videos_for_analysis") as mock_ensure:
            run_analysis(session, project, mode="provider_yolo")

        mock_ensure.assert_not_called()
        mock_live_analysis.assert_called_once()


if __name__ == "__main__":
    unittest.main()
