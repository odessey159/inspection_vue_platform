from __future__ import annotations

import runpy
import tempfile
import unittest
import unittest.mock
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.llm_output_log import build_llm_log_text, write_llm_log


class LlmOutputLogTest(unittest.TestCase):
    def test_write_llm_log_creates_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_dir = Path(tmp_dir) / "LLM_log"
            with unittest.mock.patch("app.services.llm_output_log.LLM_LOG_DIR", log_dir):
                log_path = write_llm_log(
                    source="rtsp_monitor",
                    clip_index="local-demo_000001",
                    model="qwen3.5-plus",
                    raw_response='{"findings":[{"rule_id":"rule-001","confidence":0.9,"start_offset_sec":1.0,"end_offset_sec":3.0,"description":"missing helmet"}],"notes":["ok"]}',
                    parsed_payload={
                        "findings": [
                            {
                                "rule_id": "rule-001",
                                "confidence": 0.9,
                                "start_offset_sec": 1.0,
                                "end_offset_sec": 3.0,
                                "description": "missing helmet",
                            }
                        ],
                        "notes": ["ok"],
                    },
                    notes=["segment review"],
                    diagnostics=[],
                    prompt_section="YOLO detections: person",
                    extra={"segment_index": 1, "yolo_detections": 2},
                )

            self.assertTrue(log_path.is_file())
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("=== LLM Review Output ===", content)
            self.assertIn("rule_id=rule-001", content)
            self.assertIn("raw_response:", content)
            self.assertIn("payload_json:", content)
            self.assertIn("segment001", log_path.name)

    def test_build_llm_log_text_includes_metadata(self) -> None:
        content = build_llm_log_text(
            source="batch_clip",
            clip_index="clip-00",
            model="qwen3.5-plus",
            raw_response='{"findings":[],"notes":[]}',
            parsed_payload={"findings": [], "notes": []},
            notes=[],
            diagnostics=["timeout"],
            extra={"clip_index": 0},
        )
        self.assertIn("source: batch_clip", content)
        self.assertIn("diagnostics:", content)
        self.assertIn("timeout", content)


if __name__ == "__main__":
    unittest.main()
