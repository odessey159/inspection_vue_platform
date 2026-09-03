from __future__ import annotations

import runpy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.models import Finding, Project
from app.services.import_pipeline import (
    ensure_rtsp_vehicle_workspace,
    get_project_by_vehicle_id,
    list_projects,
    prepare_vehicle_workspace,
)
from app.services.rtsp_recorder import resolve_project_rtsp_url, resolve_project_vehicle_id
from app.services.rtsp_vehicles import RtspVehicle
from app.services.storage import ensure_project_dirs, project_workspace_id


class VehicleWorkspacePathTests(unittest.TestCase):
    def test_ensure_project_dirs_uses_robots_vehicle_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots = Path(tmp_dir) / "robots"
            with patch("app.services.storage.ROBOTS_DIR", robots):
                dirs = ensure_project_dirs("local-demo")
            self.assertEqual(dirs["root"], robots / "local-demo")
            self.assertTrue((robots / "local-demo" / "artifacts").is_dir())
            self.assertTrue((robots / "local-demo" / "scenes").is_dir())

    def test_project_workspace_id_prefers_vehicle_id_over_lidar_topic(self) -> None:
        project = Project(
            name="ws",
            vehicle_id="local-demo",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="",
            point_topic="/lidar/data",
        )
        self.assertEqual(project_workspace_id(project), "local-demo")


class VehicleWorkspaceUpsertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def test_prepare_vehicle_workspace_reuses_row_for_same_vehicle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots = Path(tmp_dir) / "robots"
            with patch("app.services.storage.ROBOTS_DIR", robots):
                with patch("app.services.storage.PROJECTS_DIR", Path(tmp_dir) / "projects"):
                    with Session(self.engine) as session:
                        first = prepare_vehicle_workspace(
                            session,
                            name="first",
                            bag_dir="rtsp://127.0.0.1:18554/live",
                            standards_dir="/tmp/standards",
                            vehicle_id="local-demo",
                            rtsp_vehicle=True,
                        )
                        first_id = first.id
                        finding = Finding(
                            project_id=first_id or 0,
                            finding_uid="preserve-on-reimport",
                            rule_id="rule-1",
                            title="existing finding",
                            time_start_ms=0,
                            time_end_ms=1000,
                            description="must survive workspace preparation",
                            confidence=0.9,
                        )
                        session.add(finding)
                        first.findings_count = 1
                        session.add(first)
                        session.commit()
                        finding_id = finding.id
                        second = prepare_vehicle_workspace(
                            session,
                            name="second",
                            bag_dir="rtsp://10.0.0.52:8554/robot",
                            standards_dir="/tmp/standards",
                            vehicle_id="local-demo",
                            rtsp_vehicle=True,
                        )
                        self.assertEqual(second.id, first_id)
                        self.assertEqual(second.name, "second")
                        self.assertEqual(second.bag_dir, "rtsp://10.0.0.52:8554/robot")
                        self.assertEqual(second.findings_count, 1)
                        self.assertIsNotNone(session.get(Finding, finding_id))
                        fetched = get_project_by_vehicle_id(session, "local-demo")
                        self.assertIsNotNone(fetched)
                        self.assertEqual(fetched.id if fetched is not None else None, first_id)
                        self.assertTrue((robots / "local-demo" / "artifacts").is_dir())

    def test_list_projects_returns_one_row_per_vehicle(self) -> None:
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            session.add(
                Project(
                    name="old",
                    vehicle_id="local-demo",
                    bag_dir="rtsp://127.0.0.1:18554/live",
                    standards_dir="/tmp/standards",
                    artifacts_dir="",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    name="new",
                    vehicle_id="local-demo",
                    bag_dir="rtsp://10.0.0.52:8554/robot",
                    standards_dir="/tmp/standards",
                    artifacts_dir="",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            listed = list_projects(session)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].vehicle_id, "local-demo")


class VehicleLiveUrlTests(unittest.TestCase):
    def test_resolve_project_rtsp_url_reads_yaml_not_frozen_bag_dir(self) -> None:
        project = Project(
            name="ws",
            vehicle_id="local-demo",
            bag_dir="rtsp://127.0.0.1:18554/live",
            standards_dir="/tmp/standards",
            artifacts_dir="",
        )
        vehicle = RtspVehicle(
            id="local-demo",
            name="本地测试巡检车",
            rtsp_url="rtsp://10.0.0.52:8554/robot",
        )
        with patch("app.services.rtsp_vehicles.get_vehicle_by_id", return_value=vehicle):
            self.assertEqual(resolve_project_vehicle_id(project), "local-demo")
            self.assertEqual(resolve_project_rtsp_url(project), "rtsp://10.0.0.52:8554/robot")


class EnsureRtspVehicleWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.vehicle = RtspVehicle(
            id="local-demo",
            name="本地测试巡检车",
            rtsp_url="rtsp://10.0.0.52:8554/robot",
        )

    def test_creates_workspace_without_a_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots = Path(tmp_dir) / "robots"
            standards = Path(tmp_dir) / "standards"
            with patch("app.services.storage.ROBOTS_DIR", robots):
                with patch("app.services.import_pipeline.DEFAULT_STANDARDS_DIR", standards):
                    with patch("app.services.rtsp_vehicles.get_vehicle_by_id", return_value=self.vehicle):
                        with Session(self.engine) as session:
                            project = ensure_rtsp_vehicle_workspace(session, "local-demo")
                            self.assertIsNotNone(project.id)
                            self.assertEqual(project.vehicle_id, "local-demo")
                            self.assertEqual(project.bag_dir, self.vehicle.rtsp_url)
                            self.assertEqual(project.status, "watching")
                            self.assertEqual(project.findings_count, 0)
                            self.assertTrue((robots / "local-demo" / "artifacts").is_dir())

    def test_does_not_clear_existing_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            robots = Path(tmp_dir) / "robots"
            standards = Path(tmp_dir) / "standards"
            with patch("app.services.storage.ROBOTS_DIR", robots):
                with patch("app.services.import_pipeline.DEFAULT_STANDARDS_DIR", standards):
                    with patch("app.services.rtsp_vehicles.get_vehicle_by_id", return_value=self.vehicle):
                        with Session(self.engine) as session:
                            project = ensure_rtsp_vehicle_workspace(session, "local-demo")
                            finding = Finding(
                                project_id=project.id or 0,
                                finding_uid="keep-me",
                                rule_id="rule-1",
                                title="kept",
                                time_start_ms=0,
                                time_end_ms=1000,
                                description="stay",
                                confidence=0.9,
                            )
                            session.add(finding)
                            project.findings_count = 1
                            session.add(project)
                            session.commit()
                            finding_id = finding.id
                            updated = ensure_rtsp_vehicle_workspace(session, "local-demo")
                            self.assertEqual(updated.id, project.id)
                            self.assertEqual(updated.findings_count, 1)
                            kept = session.get(Finding, finding_id)
                            self.assertIsNotNone(kept)
                            self.assertEqual(kept.finding_uid if kept is not None else None, "keep-me")


if __name__ == "__main__":
    unittest.main()
