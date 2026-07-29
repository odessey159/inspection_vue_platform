from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.scene_transport import (  # noqa: E402
    compact_scene_payload,
    load_compact_scene_json,
    select_primary_points,
)


class SceneTransportTests(unittest.TestCase):
    def test_compact_keeps_single_point_layer(self) -> None:
        points = [[float(i), 0.0, 0.0, 1.0] for i in range(100)]
        payload = {
            "points": points,
            "full_points": points,
            "roof_removed_points": points,
            "floor_removed_points": points,
            "structure_points": points,
            "render_points": points,
            "trajectory": [[0, 0, 0]],
            "trajectory_timestamps": [1],
            "notes": [],
            "scene_quality": {},
            "raw_point_count": 100,
            "render_point_count": 100,
            "structure_point_count": 100,
        }
        compact = compact_scene_payload(payload)
        self.assertEqual(len(compact["render_points"]), 100)
        for key in ("points", "full_points", "roof_removed_points", "floor_removed_points", "structure_points"):
            self.assertEqual(compact[key], [])
        encoded = json.dumps(compact, separators=(",", ":"))
        full = json.dumps(payload, separators=(",", ":"))
        self.assertLess(len(encoded) * 2, len(full))

    def test_load_compact_scene_json_writes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            map_path = Path(tmp_dir) / "scene.json"
            points = [[1.0, 2.0, 3.0, 4.0]] * 50
            map_path.write_text(
                json.dumps(
                    {
                        "points": points,
                        "full_points": points,
                        "roof_removed_points": points,
                        "render_points": points,
                        "structure_points": points,
                        "floor_removed_points": [],
                        "notes": [],
                        "scene_quality": {},
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            first = load_compact_scene_json(map_path)
            cache = map_path.with_name("scene.web.json")
            self.assertTrue(cache.is_file())
            self.assertEqual(len(select_primary_points(first)), 50)
            second = load_compact_scene_json(map_path)
            self.assertEqual(len(second["render_points"]), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
