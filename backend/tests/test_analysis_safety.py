from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine, select

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.models import Finding, HazardRule, HazardZone, Project  # noqa: E402
from app.services.analysis import _attach_zone, run_analysis  # noqa: E402
from app.services.analysis_types import AnalysisRunResult, FindingSeed  # noqa: E402


def _project() -> Project:
    return Project(
        name="analysis-safety",
        status="indexed",
        vehicle_id="offline",
        bag_dir="/tmp/bag",
        standards_dir="/tmp/standards",
        artifacts_dir="/tmp/project",
        bag_start_ts=1_000,
        bag_end_ts=61_000,
    )


def _rule(project_id: int) -> HazardRule:
    return HazardRule(
        project_id=project_id,
        rule_id="rule-001",
        domain="industrial-inspection",
        category="ppe",
        object_name="person",
        check_item="helmet",
        checker_scope="visual",
        hazard_desc="Missing helmet",
        legal_basis="",
        evidence_objects_json='["person"]',
        severity="high",
        visual_detectable=True,
    )


def _finding(project_id: int, uid: str = "old-result") -> Finding:
    return Finding(
        project_id=project_id,
        finding_uid=uid,
        rule_id="rule-001",
        title="old",
        time_start_ms=2_000,
        time_end_ms=3_000,
        evidence_frame_ts_json="[2000]",
        description="existing successful result",
        confidence=0.8,
        analysis_mode="provider_yolo",
    )


class AnalysisReplacementSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def _seed_project(self, session: Session) -> tuple[Project, int]:
        project = _project()
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.id is not None
        session.add(_rule(project.id))
        old = _finding(project.id)
        session.add(old)
        project.findings_count = 1
        session.add(project)
        session.commit()
        session.refresh(old)
        assert old.id is not None
        return project, old.id

    def _patch_dependencies(self, result: AnalysisRunResult):
        return (
            patch("app.services.analysis.is_rtsp_project", return_value=False),
            patch("app.services.maps.load_map_for_vehicle_id", return_value=None),
            patch("app.services.analysis.run_provider_yolo_analysis", return_value=result),
            patch("app.services.analysis.read_analysis_summary", return_value={}),
            patch("app.services.analysis.write_analysis_summary"),
            patch("app.services.analysis.cache_evidence_frames"),
        )

    def test_failed_retry_preserves_previous_batch_findings(self) -> None:
        failed = AnalysisRunResult(
            status="provider_failed",
            findings=[],
            summary={"status": "provider_failed", "diagnostics": ["YOLO unavailable"]},
        )
        with Session(self.engine) as session:
            project, old_id = self._seed_project(session)
            patches = self._patch_dependencies(failed)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                persisted = run_analysis(session, project, mode="provider_yolo")

            self.assertEqual(persisted, [])
            self.assertIsNotNone(session.get(Finding, old_id))
            session.refresh(project)
            self.assertEqual(project.status, "provider_failed")
            self.assertEqual(project.findings_count, 1)

    def test_successful_retry_replaces_previous_batch_findings(self) -> None:
        succeeded = AnalysisRunResult(
            status="provider_analyzed",
            findings=[
                FindingSeed(
                    rule_id="rule-001",
                    title="new",
                    time_start_ms=10_000,
                    time_end_ms=11_000,
                    evidence_frame_ts=[10_000],
                    description="new successful result",
                    confidence=0.95,
                    severity="high",
                )
            ],
            summary={"status": "provider_analyzed", "diagnostics": []},
        )
        with Session(self.engine) as session:
            project, old_id = self._seed_project(session)
            patches = self._patch_dependencies(succeeded)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                persisted = run_analysis(session, project, mode="provider_yolo")

            self.assertEqual(len(persisted), 1)
            rows = list(session.exec(select(Finding).where(Finding.project_id == (project.id or 0))))
            self.assertEqual([item.title for item in rows], ["new"])
            self.assertNotIn("old-result", {item.finding_uid for item in rows})

    def test_empty_trajectory_does_not_create_origin_zone(self) -> None:
        session = MagicMock()
        finding = _finding(1)
        finding.id = 9
        zone = _attach_zone(session, 1, finding, [], [], 0, 60_000)
        self.assertIsNone(zone)
        session.add.assert_not_called()
        session.flush.assert_not_called()


if __name__ == "__main__":
    unittest.main()
