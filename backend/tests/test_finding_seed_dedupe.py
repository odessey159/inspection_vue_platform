from __future__ import annotations

import runpy
import unittest
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.analysis_types import FindingSeed
from app.services.provider import _dedupe_seeds


def _seed(
    rule_id: str,
    *,
    start_ms: int,
    end_ms: int | None = None,
    confidence: float = 0.5,
) -> FindingSeed:
    return FindingSeed(
        rule_id=rule_id,
        title=rule_id,
        time_start_ms=start_ms,
        time_end_ms=end_ms if end_ms is not None else start_ms + 1000,
        evidence_frame_ts=[start_ms],
        description=rule_id,
        confidence=confidence,
        severity="high",
    )


class FindingSeedDedupeTest(unittest.TestCase):
    def test_legacy_second_rounding_keeps_higher_confidence(self) -> None:
        seeds = [
            _seed("rule-a", start_ms=1000, confidence=0.4),
            _seed("rule-a", start_ms=1400, confidence=0.9),
            _seed("rule-b", start_ms=1000, confidence=0.7),
        ]
        deduped = _dedupe_seeds(seeds)
        self.assertEqual(len(deduped), 2)
        by_rule = {item.rule_id: item for item in deduped}
        self.assertAlmostEqual(by_rule["rule-a"].confidence, 0.9)

    def test_same_time_window_collapses_mosaic_views(self) -> None:
        seeds = [
            _seed("rule-a", start_ms=10_000, confidence=0.6),
            _seed("rule-a", start_ms=10_400, confidence=0.81),
            _seed("rule-a", start_ms=11_100, confidence=0.7),
            _seed("rule-b", start_ms=10_000, confidence=0.5),
        ]
        deduped = _dedupe_seeds(seeds, same_time_window_ms=2000)
        self.assertEqual(len(deduped), 2)
        rule_a = next(item for item in deduped if item.rule_id == "rule-a")
        self.assertAlmostEqual(rule_a.confidence, 0.81)
        self.assertEqual(rule_a.time_start_ms, 10_400)

    def test_same_time_window_keeps_separated_events(self) -> None:
        seeds = [
            _seed("rule-a", start_ms=0, confidence=0.8),
            _seed("rule-a", start_ms=2500, confidence=0.9),
        ]
        deduped = _dedupe_seeds(seeds, same_time_window_ms=2000)
        self.assertEqual(len(deduped), 2)


if __name__ == "__main__":
    unittest.main()
