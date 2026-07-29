from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.storage import resolve_project_path, to_project_relative_path  # noqa: E402


class ResolveProjectPathTests(unittest.TestCase):
    def test_relative_path_under_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            scene = root / "scenes" / "scene.json"
            scene.parent.mkdir(parents=True)
            scene.write_text("{}", encoding="utf-8")
            resolved = resolve_project_path(root, "scenes/scene.json", "scenes/scene.json")
            self.assertEqual(resolved, scene)

    def test_windows_absolute_path_remaps_inside_linux_style_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            scene = root / "scenes" / "scene.json"
            scene.parent.mkdir(parents=True)
            scene.write_text("{}", encoding="utf-8")

            windows_db_value = (
                r"D:\important files\internship_file\3\inspection_vue_platform"
                r"\.runtime\projects\15\scenes\scene.json"
            )
            resolved = resolve_project_path(root, windows_db_value, "scenes/scene.json")
            self.assertEqual(resolved, scene)
            self.assertTrue(resolved.exists())

    def test_missing_windows_path_falls_back_to_default_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            scene = root / "scenes" / "scene.json"
            scene.parent.mkdir(parents=True)
            scene.write_text("{}", encoding="utf-8")
            resolved = resolve_project_path(
                root,
                r"C:\missing\projects\9\scenes\other.json",
                "scenes/scene.json",
            )
            # other.json under scenes/ is recoverable; if that file is absent and default exists,
            # prefer default when recovered path does not exist.
            # Recovered relative is scenes/other.json which does not exist → default.
            self.assertEqual(resolved, scene)

    def test_to_project_relative_path_uses_posix_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            scene = root / "scenes" / "scene.json"
            scene.parent.mkdir(parents=True)
            scene.write_text("{}", encoding="utf-8")
            stored = to_project_relative_path(root, scene)
            self.assertEqual(stored, "scenes/scene.json")
            self.assertNotIn("\\", stored)

    def test_inspection_video_windows_path_remaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            video = root / "artifacts" / "inspection.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"fake")
            windows_db_value = r"D:\runtime\projects\15\artifacts\inspection.mp4"
            resolved = resolve_project_path(root, windows_db_value, "artifacts/inspection.mp4")
            self.assertEqual(resolved, video)


if __name__ == "__main__":
    unittest.main(verbosity=2)
