from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.maps import import_map, load_map_for_vehicle_id  # noqa: E402
from app.services.rtsp_sei import FrameMetadata, PictureMetadata  # noqa: E402
from app.services.rtsp_vehicles import update_vehicle_map_id  # noqa: E402
from app.services.storage import write_json  # noqa: E402
from app.services.vehicle_trajectory import (  # noqa: E402
    extend_vehicle_trajectory_from_pictures,
    extend_vehicle_trajectory_from_pose,
    load_vehicle_trajectory,
    overlay_vehicle_trajectory,
    strip_catalog_trajectory,
)


VEHICLES_YAML = """
vehicles:
  - id: local-demo
    name: 本地测试巡检车
    rtsp_url: rtsp://127.0.0.1:18554/live
  - id: vehicle-02
    name: 巡检车02
    rtsp_url: rtsp://127.0.0.1:18554/live
"""


def _tiny_scene(marker_x: float = 0.0) -> dict:
    return {
        "points": [[marker_x, 0, 0, 1]],
        "render_points": [[marker_x, 0, 0, 1]],
        "full_points": [[marker_x, 0, 0, 1]],
        "roof_removed_points": [[marker_x, 0, 0, 1]],
        "floor_removed_points": [[marker_x, 0, 0, 1]],
        "structure_points": [[marker_x, 0, 0, 1]],
        "trajectory": [[marker_x, 0, 0], [marker_x + 1, 0, 0]],
        "trajectory_timestamps": [100, 300],
        "trajectory_orientations": [[0, 0, 0, 1], [0, 0, 0, 1]],
        "bounds": {"min": [0, 0, 1.2], "max": [1, 1, 3]},
        "floor_cut_default": 1.2,
        "source_type": "pcd_accumulated_lidar_structure",
        "raw_point_count": 1,
        "render_point_count": 1,
        "notes": [],
        "scene_quality": {},
    }


def _picture(ts_ms: int, x: float, y: float, yaw: float = 0.0) -> PictureMetadata:
    return PictureMetadata(
        frame_index=0,
        metadata=FrameMetadata(timestamp_ns=int(ts_ms) * 1_000_000, x=x, y=y, yaw=yaw),
    )


class VehicleTrajectoryTests(unittest.TestCase):
    def test_strip_catalog_trajectory_drops_baked_path(self) -> None:
        stripped = strip_catalog_trajectory(_tiny_scene(3.0))
        self.assertEqual(stripped["trajectory"], [])
        self.assertEqual(stripped["trajectory_timestamps"], [])
        self.assertTrue(stripped["scene_quality"].get("catalog_pure_map"))

    def test_extend_appends_moved_poses_and_skips_stationary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots_dir = Path(tmp_dir) / "robots"
            with patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir):
                first = extend_vehicle_trajectory_from_pictures(
                    "local-demo",
                    [_picture(1_000, 0.0, 0.0), _picture(1_050, 0.01, 0.0)],
                )
                self.assertEqual(len(first.trajectory), 1)
                second = extend_vehicle_trajectory_from_pictures(
                    "local-demo",
                    [_picture(2_000, 1.5, 0.2)],
                )
                self.assertEqual(len(second.trajectory), 2)
                self.assertAlmostEqual(second.trajectory[-1][0], 1.5)
                loaded = load_vehicle_trajectory("local-demo")
                self.assertEqual(len(loaded.trajectory_timestamps), 2)
                self.assertEqual(loaded.trajectory_timestamps[-1], 2_000)

    def test_overlay_uses_vehicle_path_not_map_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            maps_dir = root / "maps"
            vehicles_path = root / "vehicles.yaml"
            override_path = root / "override.yaml"
            robots_dir = root / "robots"
            vehicles_path.write_text(VEHICLES_YAML, encoding="utf-8")
            source = root / "scene.json"
            write_json(source, _tiny_scene(4.0))
            with (
                patch("app.services.maps.MAPS_DIR", maps_dir),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_PATH", vehicles_path),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_OVERRIDE_PATH", override_path),
                patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir),
            ):
                record = import_map(source, name="plant")
                update_vehicle_map_id("local-demo", record.id)
                extend_vehicle_trajectory_from_pictures(
                    "local-demo",
                    [_picture(50_000, 8.0, 9.0), _picture(51_000, 8.4, 9.1)],
                )
                scene = load_map_for_vehicle_id("local-demo")

            self.assertIsNotNone(scene)
            assert scene is not None
            self.assertEqual(scene["trajectory_timestamps"], [50_000, 51_000])
            self.assertAlmostEqual(scene["trajectory"][0][0], 8.0)
            self.assertAlmostEqual(scene["trajectory"][0][2], 1.2)
            self.assertEqual(scene["scene_quality"].get("trajectory_source"), "rtsp_sei")
            self.assertNotEqual(scene["trajectory"], [[4.0, 0, 0], [5.0, 0, 0]])

    def test_new_stream_session_resets_path_when_timestamp_rewinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots_dir = Path(tmp_dir) / "robots"
            with patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir):
                extend_vehicle_trajectory_from_pictures(
                    "local-demo",
                    [_picture(50_000, 8.0, 9.0), _picture(51_000, 8.4, 9.1)],
                )
                restarted = extend_vehicle_trajectory_from_pose(
                    "local-demo",
                    timestamp_ms=10_000,
                    x=1.0,
                    y=2.0,
                    reset_on_time_rewind=True,
                )

            self.assertIsNotNone(restarted)
            assert restarted is not None
            self.assertEqual(restarted.trajectory_timestamps, [10_000])
            self.assertEqual(restarted.trajectory, [[1.0, 2.0, 0.0]])

    def test_overlay_empty_until_rtsp_poses_exist(self) -> None:
        scene = overlay_vehicle_trajectory(_tiny_scene(1.0), "missing-vehicle")
        self.assertEqual(scene["trajectory"], [])
        self.assertIsNone(scene["scene_quality"].get("trajectory_source"))


if __name__ == "__main__":
    unittest.main()
