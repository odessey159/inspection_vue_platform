import math
import runpy
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.dataset import PoseRecord, PoseTimeline
from app.services.scene import _resolve_pose_validity_window


def pose(timestamp_ms: int, *, x: float = 0.0, y: float = 0.0, qz: float = 0.0, qw: float = 1.0) -> PoseRecord:
    return PoseRecord(
        timestamp_ms=timestamp_ms,
        x=x,
        y=y,
        z=0.0,
        qx=0.0,
        qy=0.0,
        qz=qz,
        qw=qw,
        yaw=0.0,
    )


class PoseTimelineTest(unittest.TestCase):
    def test_interpolates_midpoint_translation_and_normalizes_quaternion(self) -> None:
        timeline = PoseTimeline(
            [
                pose(1000, x=0.0, qz=0.0, qw=1.0),
                pose(2000, x=10.0, qz=math.sin(math.pi / 4), qw=math.cos(math.pi / 4)),
            ]
        )

        sample = timeline.at(1500)

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertTrue(sample.interpolated)
        self.assertEqual(sample.source_gap_ms, 1000)
        self.assertAlmostEqual(sample.pose.x, 5.0)
        norm = math.sqrt(sample.pose.qx**2 + sample.pose.qy**2 + sample.pose.qz**2 + sample.pose.qw**2)
        self.assertAlmostEqual(norm, 1.0)

    def test_pose_timeline_does_not_extrapolate(self) -> None:
        timeline = PoseTimeline([pose(1000), pose(2000, x=1.0)])

        self.assertIsNone(timeline.at(999))
        self.assertIsNone(timeline.at(2001))

    def test_validity_window_uses_lidar_slam_reference_when_provided(self) -> None:
        primary = [pose(1000), pose(5000, x=4.0)]
        lidar_slam = [pose(2200, x=1.0), pose(4200, x=3.0)]

        window = _resolve_pose_validity_window(primary, lidar_slam, "/lidar_slam_pose")

        self.assertEqual(window.start_ms, 2200)
        self.assertEqual(window.end_ms, 4200)
        self.assertEqual(window.source, "/lidar_slam_pose")


if __name__ == "__main__":
    unittest.main()
