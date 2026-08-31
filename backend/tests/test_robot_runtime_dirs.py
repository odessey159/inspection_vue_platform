from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.rtsp_vehicles import (
    RtspVehicle,
    ensure_robot_runtime_dirs,
    robot_runtime_paths,
)


class RobotRuntimeDirsTests(unittest.TestCase):
    def test_robot_runtime_paths_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots_root = Path(tmp_dir) / "robots"
            with patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_root):
                paths = robot_runtime_paths("local-demo")
            self.assertEqual(paths.root, robots_root / "local-demo")
            self.assertEqual(paths.recordings, robots_root / "local-demo" / "recordings")
            self.assertEqual(paths.maps, robots_root / "local-demo" / "maps")

    def test_ensure_robot_runtime_dirs_creates_recordings_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots_root = Path(tmp_dir) / "robots"
            vehicles = [
                RtspVehicle(id="local-demo", name="本地测试巡检车", rtsp_url="rtsp://127.0.0.1:18554/live"),
                RtspVehicle(id="vehicle-02", name="巡检车02", rtsp_url="rtsp://127.0.0.1:18554/live"),
            ]
            with patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_root):
                created = ensure_robot_runtime_dirs(vehicles)

            self.assertEqual(len(created), 2)
            for vehicle in vehicles:
                recordings = robots_root / vehicle.id / "recordings"
                maps = robots_root / vehicle.id / "maps"
                self.assertTrue(recordings.is_dir())
                self.assertFalse(maps.is_dir())


if __name__ == "__main__":
    unittest.main()
