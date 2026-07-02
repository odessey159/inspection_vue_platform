from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..models import Project
from .storage import read_json, write_json


def read_analysis_summary(project: Project) -> dict[str, object]:
    path = analysis_summary_path(project)
    if not path.exists():
        return {}
    return read_json(path)


def write_analysis_summary(project: Project, payload: dict[str, object]) -> Path:
    normalized = {
        **payload,
        "updated_at": payload.get("updated_at") or datetime.now(timezone.utc).isoformat(),
    }
    path = analysis_summary_path(project)
    write_json(path, normalized)
    return path


def analysis_summary_path(project: Project) -> Path:
    return Path(project.artifacts_dir) / "summaries" / "analysis_summary.json"
