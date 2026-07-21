from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.rtsp_recorder import (  # noqa: E402
    RtspRecordingResult,
    align_scene_timestamps_to_video,
    build_rtsp_scene_for_recording,
)
from app.services.rtsp_vehicles import find_robot_map_scene, robot_runtime_paths  # noqa: E402
from app.services.storage import write_json  # noqa: E402


class MapVideoTimestampSyncTests(unittest.TestCase):
    def test_align_scene_timestamps_to_video_linear_remap(self) -> None:
        scene = {
            "trajectory_timestamps": [1000, 2000, 3000],
            "hazard_zones": [{"related_pose_ts": 2000}],
            "notes": [],
            "scene_quality": {},
        }
        aligned = align_scene_timestamps_to_video(scene, 10_000, 12_000, map_source="/maps/scene.json")
        self.assertEqual(aligned["trajectory_timestamps"], [10_000, 11_000, 12_000])
        self.assertEqual(aligned["hazard_zones"][0]["related_pose_ts"], 11_000)
        self.assertEqual(aligned["scene_quality"]["time_alignment"]["mode"], "linear_map_to_video")

    def test_build_rtsp_scene_uses_robot_map_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots_root = Path(tmp_dir) / "robots"
            with patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_root):
                maps_dir = robot_runtime_paths("local-demo").maps
                maps_dir.mkdir(parents=True)
                write_json(
                    maps_dir / "scene.json",
                    {
                        "points": [[0, 0, 0, 1]],
                        "render_points": [[0, 0, 0, 1]],
                        "trajectory": [[0, 0, 0], [1, 0, 0]],
                        "trajectory_timestamps": [100, 300],
                        "trajectory_orientations": [[0, 0, 0, 1], [0, 0, 0, 1]],
                        "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]},
                        "source_type": "pcd_accumulated_lidar_structure",
                        "notes": [],
                        "scene_quality": {},
                    },
                )
                self.assertIsNotNone(find_robot_map_scene("local-demo"))

                recording = RtspRecordingResult(
                    rtsp_url="rtsp://127.0.0.1:18554/live",
                    output_path=Path(tmp_dir) / "recording.mp4",
                    playback_path=Path(tmp_dir) / "inspection.mp4",
                    duration_sec=20.0,
                    fps=10.0,
                    frame_count=200,
                    video_start_ts=50_000,
                    video_end_ts=70_000,
                )
                with patch(
                    "app.services.rtsp_recorder.resolve_storage_key_for_rtsp_url",
                    return_value="local-demo",
                ):
                    scene = build_rtsp_scene_for_recording(recording)

            self.assertEqual(scene["source_type"], "pcd_accumulated_lidar_structure")
            self.assertEqual(scene["trajectory_timestamps"], [50_000, 70_000])
            self.assertNotEqual(scene["source_type"], "rtsp_placeholder")


if __name__ == "__main__":
    unittest.main()
