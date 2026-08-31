from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.maps import import_map  # noqa: E402
from app.services.rtsp_recorder import (  # noqa: E402
    RtspRecordingResult,
    align_scene_timestamps_to_video,
    build_rtsp_scene_for_recording,
)
from app.services.rtsp_vehicles import update_vehicle_map_id  # noqa: E402
from app.services.storage import write_json  # noqa: E402


VEHICLES_YAML = """
vehicles:
  - id: local-demo
    name: 本地测试巡检车
    rtsp_url: rtsp://127.0.0.1:18554/live
  - id: vehicle-02
    name: 巡检车02
    rtsp_url: rtsp://127.0.0.1:18554/live
"""


def _scene(marker_x: float) -> dict:
    return {
        "points": [[marker_x, 0, 0, 1]],
        "render_points": [[marker_x, 0, 0, 1]],
        "trajectory": [[marker_x, 0, 0], [marker_x + 1, 0, 0]],
        "trajectory_timestamps": [100, 300],
        "trajectory_orientations": [[0, 0, 0, 1], [0, 0, 0, 1]],
        "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]},
        "source_type": "pcd_accumulated_lidar_structure",
        "notes": [],
        "scene_quality": {},
    }


class MapVideoTimestampSyncTests(unittest.TestCase):
    def test_align_scene_timestamps_to_video_linear_remap(self) -> None:
        scene = {
            "trajectory_timestamps": [1000, 2000, 3000],
            "hazard_zones": [{"related_pose_ts": 2000}],
            "notes": [],
            "scene_quality": {},
        }
        aligned = align_scene_timestamps_to_video(scene, 10_000, 12_000, map_source="catalog:plant")
        self.assertEqual(aligned["trajectory_timestamps"], [10_000, 11_000, 12_000])
        self.assertEqual(aligned["hazard_zones"][0]["related_pose_ts"], 11_000)
        self.assertEqual(aligned["scene_quality"]["time_alignment"]["mode"], "linear_map_to_video")

    def test_build_rtsp_scene_uses_vehicle_map_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            maps_dir = root / "maps"
            vehicles_path = root / "vehicles.yaml"
            override_path = root / "override.yaml"
            robots_dir = root / "robots"
            vehicles_path.write_text(VEHICLES_YAML, encoding="utf-8")
            source_a = root / "map-a-scene.json"
            source_b = root / "map-b-scene.json"
            write_json(source_a, _scene(0.0))
            write_json(source_b, _scene(2.0))

            recording = RtspRecordingResult(
                rtsp_url="rtsp://127.0.0.1:18554/live",
                output_path=root / "recording.mp4",
                playback_path=root / "inspection.mp4",
                duration_sec=20.0,
                fps=10.0,
                frame_count=200,
                video_start_ts=50_000,
                video_end_ts=70_000,
            )

            with (
                patch("app.services.maps.MAPS_DIR", maps_dir),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_PATH", vehicles_path),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_OVERRIDE_PATH", override_path),
                patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir),
            ):
                map_a = import_map(source_a, name="map-a")
                map_b = import_map(source_b, name="map-b")
                update_vehicle_map_id("local-demo", map_a.id)
                update_vehicle_map_id("vehicle-02", map_b.id)
                scene = build_rtsp_scene_for_recording(recording, vehicle_id="vehicle-02")

            self.assertEqual(scene["source_type"], "pcd_accumulated_lidar_structure")
            self.assertEqual(scene["render_points"][0][0], 2.0)
            self.assertEqual(scene["trajectory"], [])
            self.assertEqual(scene["trajectory_timestamps"], [])
            quality = scene["scene_quality"]
            self.assertEqual(quality.get("map_id"), map_b.id)
            self.assertTrue(quality.get("catalog_pure_map"))

    def test_build_rtsp_scene_placeholder_when_vehicle_has_no_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            maps_dir = root / "maps"
            vehicles_path = root / "vehicles.yaml"
            override_path = root / "override.yaml"
            robots_dir = root / "robots"
            vehicles_path.write_text(VEHICLES_YAML, encoding="utf-8")
            recording = RtspRecordingResult(
                rtsp_url="rtsp://127.0.0.1:18554/live",
                output_path=root / "recording.mp4",
                playback_path=root / "inspection.mp4",
                duration_sec=20.0,
                fps=10.0,
                frame_count=200,
                video_start_ts=50_000,
                video_end_ts=70_000,
            )
            with (
                patch("app.services.maps.MAPS_DIR", maps_dir),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_PATH", vehicles_path),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_OVERRIDE_PATH", override_path),
                patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir),
            ):
                scene = build_rtsp_scene_for_recording(recording, vehicle_id="local-demo")

            self.assertEqual(scene["source_type"], "rtsp_placeholder")


if __name__ == "__main__":
    unittest.main()
