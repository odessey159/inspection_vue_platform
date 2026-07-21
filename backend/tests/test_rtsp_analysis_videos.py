from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.analysis_types import AnalysisVideoTarget, dedupe_analysis_video_targets


class RtspAnalysisVideosTest(unittest.TestCase):
    def test_dedupe_analysis_video_targets_keeps_distinct_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = root / "previous.mp4"
            second = root / "current.mp4"
            first.write_bytes(b"a")
            second.write_bytes(b"b")

            targets = dedupe_analysis_video_targets(
                [
                    AnalysisVideoTarget(path=first, label="previous", video_start_ts=1, video_end_ts=2),
                    AnalysisVideoTarget(path=second, label="current", video_start_ts=3, video_end_ts=4),
                    AnalysisVideoTarget(path=first, label="previous-copy", video_start_ts=1, video_end_ts=2),
                ]
            )

            self.assertEqual(len(targets), 2)
            self.assertEqual({target.label for target in targets}, {"previous", "current"})


if __name__ == "__main__":
    unittest.main()
