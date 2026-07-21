import runpy
import unittest
from pathlib import Path

import numpy as np

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.scene import CloudPoint, EgoArtifactFilter, Transform3D, _filter_ego_artifacts, _filter_trajectory_sweep_artifacts


class EgoArtifactFilterTest(unittest.TestCase):
    def test_fixed_vehicle_bounds_are_filtered_without_learned_voxels(self) -> None:
        voxel_size = 0.12
        ego_filter = EgoArtifactFilter(
            enabled=True,
            voxel_size=voxel_size,
            min_hit_ratio=0.08,
            min_hit_count=0,
            sampled_frame_count=8,
            candidate_point_count=24,
            core_voxel_count=0,
            expanded_voxel_count=0,
            candidate_bounds={"min": [-2.35, -1.75, -0.45], "max": [2.35, 1.75, 1.65]},
            artifact_voxels=frozenset(),
        )
        mount = Transform3D(
            rotation=np.eye(3, dtype=np.float64),
            translation=np.zeros(3, dtype=np.float64),
            source="test",
        )
        raw_points = np.asarray(
            [
                [0.25, 0.02, 0.50, 80.0],
                [3.0, 0.0, 1.0, 15.0],
            ],
            dtype=np.float32,
        )

        filtered_points, removed_count = _filter_ego_artifacts(raw_points, ego_filter, mount)

        self.assertEqual(removed_count, 1)
        self.assertEqual(filtered_points.shape[0], 1)
        self.assertFalse(np.any(np.isclose(filtered_points[:, 0], 0.25)))

    def test_trajectory_sweep_removes_low_residuals_but_keeps_high_structure(self) -> None:
        points = [
            CloudPoint(x=0.1, y=0.1, z=0.8, intensity=1.0, density=1.0),
            CloudPoint(x=0.1, y=0.1, z=3.0, intensity=1.0, density=1.0),
            CloudPoint(x=5.0, y=5.0, z=0.8, intensity=1.0, density=1.0),
        ]
        trajectory = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]

        filtered_points, removed_count = _filter_trajectory_sweep_artifacts(points, trajectory)

        self.assertEqual(removed_count, 1)
        self.assertEqual(len(filtered_points), 2)
        self.assertEqual([point.z for point in filtered_points], [3.0, 0.8])


if __name__ == "__main__":
    unittest.main()
