from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import Session, delete

from ..db import create_db_and_tables, engine
from ..models import Finding, HazardRule, HazardZone, Project
from ..settings import PROJECTS_DIR, RUNTIME_DIR
from .storage import path_size_bytes, remove_paths


def compact_project_runtime(project_root: Path) -> dict[str, int]:
    removable = [
        project_root / "extracted" / "camera_lidar_pairs",
        project_root / "manifests",
    ]
    removed_bytes = remove_paths(removable)
    return {
        "removed_bytes": removed_bytes,
        "removed_paths": sum(1 for path in removable if not path.exists()),
    }


def reset_runtime_storage() -> dict[str, int | str]:
    removed_bytes = path_size_bytes(RUNTIME_DIR)
    removed_project_dirs = sum(1 for path in PROJECTS_DIR.iterdir() if path.is_dir()) if PROJECTS_DIR.exists() else 0

    engine.dispose()
    try:
        if RUNTIME_DIR.exists():
            shutil.rmtree(RUNTIME_DIR)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        create_db_and_tables()
    except PermissionError:
        remove_paths([PROJECTS_DIR])
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        _clear_database_rows()
        create_db_and_tables()

    return {
        "status": "reset",
        "removed_project_dirs": removed_project_dirs,
        "removed_bytes": removed_bytes,
    }


def _clear_database_rows() -> None:
    with Session(engine) as session:
        session.exec(delete(HazardZone))
        session.exec(delete(Finding))
        session.exec(delete(HazardRule))
        session.exec(delete(Project))
        session.commit()
