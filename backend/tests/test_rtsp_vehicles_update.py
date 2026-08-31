from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.models import Project
from app.routers.projects import _sync_vehicle_workspace_url, patch_rtsp_vehicle
from app.schemas import RtspVehicleUpdateRequest
from app.services.rtsp_vehicles import RtspVehicle, load_rtsp_vehicles, update_vehicle_rtsp_url


BASE_YAML = """
vehicles:
  - id: local-demo
    name: 本地测试巡检车
    rtsp_url: rtsp://10.0.0.52:8554/robot
  - id: vehicle-02
    name: 巡检车02
    rtsp_url: rtsp://127.0.0.1:18554/live
"""


class RtspVehicleUrlUpdateTests(unittest.TestCase):
    def _patch_paths(self, tmp_dir: str):
        root = Path(tmp_dir)
        base_path = root / "rtsp_vehicles.yaml"
        override_path = root / "rtsp_vehicles.override.yaml"
        robots_dir = root / "robots"
        base_path.write_text(BASE_YAML, encoding="utf-8")
        return (
            patch("app.services.rtsp_vehicles.RTSP_VEHICLES_PATH", base_path),
            patch("app.services.rtsp_vehicles.RTSP_VEHICLES_OVERRIDE_PATH", override_path),
            patch("app.services.rtsp_vehicles.ROBOTS_DIR", robots_dir),
            base_path,
            override_path,
            robots_dir,
        )

    def test_override_file_wins_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path_a, path_b, path_c, _base, override_path, _robots = self._patch_paths(tmp_dir)
            override_path.write_text(
                "vehicles:\n  - id: local-demo\n    rtsp_url: rtsp://192.168.1.10:8554/front\n",
                encoding="utf-8",
            )
            with path_a, path_b, path_c:
                vehicles = load_rtsp_vehicles()
            local_demo = next(item for item in vehicles if item.id == "local-demo")
            other = next(item for item in vehicles if item.id == "vehicle-02")
            self.assertEqual(local_demo.rtsp_url, "rtsp://192.168.1.10:8554/front")
            self.assertEqual(local_demo.name, "本地测试巡检车")
            self.assertEqual(other.rtsp_url, "rtsp://127.0.0.1:18554/live")

    def test_update_writes_override_without_changing_base_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path_a, path_b, path_c, base_path, override_path, robots_dir = self._patch_paths(tmp_dir)
            original = base_path.read_text(encoding="utf-8")
            with path_a, path_b, path_c:
                updated = update_vehicle_rtsp_url("local-demo", "rtsp://10.0.0.99:8554/robot")
                vehicles = load_rtsp_vehicles()
            self.assertEqual(updated.rtsp_url, "rtsp://10.0.0.99:8554/robot")
            self.assertEqual(next(item.rtsp_url for item in vehicles if item.id == "local-demo"), "rtsp://10.0.0.99:8554/robot")
            self.assertEqual(base_path.read_text(encoding="utf-8"), original)
            self.assertTrue(override_path.is_file())
            self.assertIn("10.0.0.99", override_path.read_text(encoding="utf-8"))
            self.assertTrue((robots_dir / "local-demo" / "recordings").is_dir())

    def test_reverting_to_base_url_removes_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path_a, path_b, path_c, _base, override_path, _robots = self._patch_paths(tmp_dir)
            with path_a, path_b, path_c:
                update_vehicle_rtsp_url("local-demo", "rtsp://10.0.0.99:8554/robot")
                self.assertTrue(override_path.is_file())
                update_vehicle_rtsp_url("local-demo", "rtsp://10.0.0.52:8554/robot")
                vehicles = load_rtsp_vehicles()
            self.assertFalse(override_path.exists())
            self.assertEqual(
                next(item.rtsp_url for item in vehicles if item.id == "local-demo"),
                "rtsp://10.0.0.52:8554/robot",
            )

    def test_unknown_id_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path_a, path_b, path_c, *_ = self._patch_paths(tmp_dir)
            with path_a, path_b, path_c:
                with self.assertRaises(KeyError):
                    update_vehicle_rtsp_url("missing-car", "rtsp://127.0.0.1:18554/live")

    def test_invalid_url_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path_a, path_b, path_c, *_ = self._patch_paths(tmp_dir)
            with path_a, path_b, path_c:
                with self.assertRaises(ValueError):
                    update_vehicle_rtsp_url("local-demo", "http://127.0.0.1/live")


class SyncVehicleWorkspaceUrlTests(unittest.TestCase):
    def test_updates_rtsp_project_bag_dir_for_vehicle(self) -> None:
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                Project(
                    name="rtsp-car",
                    vehicle_id="local-demo",
                    bag_dir="rtsp://10.0.0.52:8554/robot",
                    standards_dir="/tmp/standards",
                    artifacts_dir="",
                )
            )
            session.add(
                Project(
                    name="offline",
                    vehicle_id="local-demo",
                    bag_dir="/tmp/scene.json",
                    standards_dir="/tmp/standards",
                    artifacts_dir="",
                )
            )
            session.commit()
            _sync_vehicle_workspace_url(session, "local-demo", "rtsp://10.0.0.99:8554/robot")
            rows = session.exec(select(Project)).all()
            by_name = {row.name: row.bag_dir for row in rows}
            self.assertEqual(by_name["rtsp-car"], "rtsp://10.0.0.99:8554/robot")
            self.assertEqual(by_name["offline"], "/tmp/scene.json")


class PatchRtspVehicleRouteTests(unittest.TestCase):
    def test_unknown_vehicle_returns_404(self) -> None:
        with patch("app.routers.projects.update_vehicle_rtsp_url", side_effect=KeyError("Unknown RTSP vehicle id: nope")):
            with self.assertRaises(HTTPException) as ctx:
                patch_rtsp_vehicle(
                    "nope",
                    RtspVehicleUpdateRequest(rtsp_url="rtsp://127.0.0.1:18554/live"),
                    session=MagicMock(),
                )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_invalid_url_returns_400(self) -> None:
        with patch("app.routers.projects.update_vehicle_rtsp_url", side_effect=ValueError("RTSP URL must start with rtsp://")):
            with self.assertRaises(HTTPException) as ctx:
                patch_rtsp_vehicle(
                    "local-demo",
                    RtspVehicleUpdateRequest(rtsp_url="rtsp://bad"),
                    session=MagicMock(),
                )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_success_syncs_workspace_and_restarts_watch(self) -> None:
        updated = RtspVehicle(id="local-demo", name="本地测试巡检车", rtsp_url="rtsp://10.0.0.99:8554/robot")
        session = MagicMock()
        with patch("app.routers.projects.update_vehicle_rtsp_url", return_value=updated), patch(
            "app.routers.projects._sync_vehicle_workspace_url"
        ) as sync, patch("app.routers.projects.restart_watch_for_vehicle") as restart:
            result = patch_rtsp_vehicle(
                "local-demo",
                RtspVehicleUpdateRequest(rtsp_url=updated.rtsp_url),
                session=session,
            )
        sync.assert_called_once_with(session, "local-demo", updated.rtsp_url)
        restart.assert_called_once_with("local-demo")
        self.assertEqual(result.id, "local-demo")
        self.assertEqual(result.rtsp_url, updated.rtsp_url)
