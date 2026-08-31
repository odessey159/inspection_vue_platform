from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.maps import import_map, list_maps, load_map_scene, migrate_legacy_vehicle_maps
from app.services.rtsp_vehicles import load_rtsp_vehicles, update_vehicle_map_id
from app.services.storage import write_json


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
        "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]},
        "source_type": "pcd_accumulated_lidar_structure",
        "raw_point_count": 1,
        "render_point_count": 1,
        "notes": [],
        "scene_quality": {},
    }


class MapCatalogTests(unittest.TestCase):
    def test_import_compacts_duplicate_layers_and_reuses_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            maps_dir = root / "maps"
            source = root / "scene.json"
            write_json(source, _tiny_scene(1.5))
            with patch("app.services.maps.MAPS_DIR", maps_dir):
                first = import_map(source, name="plant-a")
                second = import_map(source, name="plant-a-copy")
                payload = load_map_scene(first.id)
                self.assertEqual(first.id, second.id)
                self.assertEqual(len(list_maps()), 1)
                self.assertEqual(payload["render_points"][0][0], 1.5)
                self.assertEqual(payload["points"], [])
                self.assertEqual(payload["trajectory"], [])
                self.assertEqual(payload["trajectory_timestamps"], [])
                self.assertEqual(payload["scene_quality"]["transport"], "compact_single_layer")
                self.assertTrue(payload["scene_quality"].get("catalog_pure_map"))

    def test_vehicle_stores_index_not_a_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            maps_dir = root / "maps"
            vehicles_path = root / "vehicles.yaml"
            override_path = root / "override.yaml"
            robots_dir = root / "robots"
            source = root / "scene.json"
            vehicles_path.write_text(VEHICLES_YAML, encoding="utf-8")
            write_json(source, _tiny_scene(4.0))
            with (
                patch("app.services.maps.MAPS_DIR", maps_dir),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_PATH", vehicles_path),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_OVERRIDE_PATH", override_path),
                patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir),
            ):
                record = import_map(source, name="shared")
                updated = update_vehicle_map_id("local-demo", record.id)
                other = update_vehicle_map_id("vehicle-02", record.id)
                vehicles = load_rtsp_vehicles()

            self.assertEqual(updated.map_id, record.id)
            self.assertEqual(other.map_id, record.id)
            self.assertEqual({item.map_id for item in vehicles}, {record.id})
            self.assertTrue((maps_dir / record.id / "scene.json").is_file())
            self.assertFalse((robots_dir / "local-demo" / "maps" / "scene.json").exists())

    def test_migrate_legacy_vehicle_maps_dedupes_by_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            maps_dir = root / "maps"
            vehicles_path = root / "vehicles.yaml"
            override_path = root / "override.yaml"
            robots_dir = root / "robots"
            vehicles_path.write_text(VEHICLES_YAML, encoding="utf-8")
            scene = _tiny_scene(2.0)
            for vehicle_id in ("local-demo", "vehicle-02"):
                dest = robots_dir / vehicle_id / "maps"
                dest.mkdir(parents=True)
                write_json(dest / "scene.json", scene)

            with (
                patch("app.services.maps.MAPS_DIR", maps_dir),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_PATH", vehicles_path),
                patch("app.services.rtsp_vehicles.RTSP_VEHICLES_OVERRIDE_PATH", override_path),
                patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir),
            ):
                imported = migrate_legacy_vehicle_maps()
                vehicles = load_rtsp_vehicles()
                catalog = list_maps()

            self.assertEqual(len(imported), 1)
            self.assertEqual(len(catalog), 1)
            self.assertEqual({item.map_id for item in vehicles}, {catalog[0].id})


if __name__ == "__main__":
    unittest.main()
