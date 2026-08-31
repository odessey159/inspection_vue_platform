import runpy
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.import_pipeline import import_project, is_static_scene_import_source, resolve_scene_path
from app.services.storage import read_json

SAMPLE_SCENE = Path(__file__).resolve().parent / "pcd" / "scene.json"
SAMPLE_PCD_DIR = Path(__file__).resolve().parent / "pcd"


class StaticSceneJsonTest(unittest.TestCase):
    def test_sample_scene_json_exists_and_is_valid(self) -> None:
        self.assertTrue(SAMPLE_SCENE.is_file(), f"Missing sample scene: {SAMPLE_SCENE}")
        payload = read_json(SAMPLE_SCENE)
        self.assertGreater(len(payload.get("points", [])), 100)
        self.assertGreater(len(payload.get("render_points", [])), 100)
        self.assertIn("bounds", payload)

    def test_resolve_scene_path_accepts_file_and_directory(self) -> None:
        resolved = resolve_scene_path(SAMPLE_SCENE)
        self.assertEqual(resolved, SAMPLE_SCENE.resolve())
        self.assertTrue(is_static_scene_import_source(SAMPLE_PCD_DIR))
        self.assertTrue(is_static_scene_import_source(SAMPLE_SCENE))

    def test_import_project_rejects_scene_json_as_workspace(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            import_project(None, "map-as-project", SAMPLE_SCENE, SAMPLE_PCD_DIR)
        self.assertIn("独立点云地图", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
